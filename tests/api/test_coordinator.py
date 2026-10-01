"""Tests for api/coordinator.py: spacing, reauth, 429 backoff, retention."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dhl.api.client import (
    DHLUnifiedError,
    DHLUnifiedKeyError,
    DHLUnifiedNotFound,
    DHLUnifiedRateLimitError,
)
from custom_components.dhl.api.coordinator import DHLUnifiedCoordinator
from custom_components.dhl.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DIRECTION,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DHL_UNIFIED_BACKOFF_BASE_SECONDS,
    DHL_UNIFIED_MIN_REQUEST_GAP_SECONDS,
    DOMAIN,
    SOURCE_API,
    ParcelStatus,
)

from .payloads import body, event, shipment

CODE_A = "JJD000390007000000001"
CODE_B = "JJD000390007000000002"


def _entry(codes, **options) -> MockConfigEntry:
    parcels = [
        c if isinstance(c, dict) else {CONF_TRACKING_CODE: c} for c in codes
    ]
    return MockConfigEntry(
        domain=DOMAIN,
        title="API (abcdef)",
        unique_id="api:abcdef123456",
        data={CONF_SOURCE: SOURCE_API, "api_key": "k"},
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
            CONF_PARCELS: parcels,
            **options,
        },
    )


def _coordinator(hass, entry, responses) -> tuple[DHLUnifiedCoordinator, MagicMock]:
    """``responses`` maps a code to a body, an exception, or a list of either."""
    client = MagicMock()

    async def get(code):
        value = responses[code]
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    client.async_get_shipments = AsyncMock(side_effect=get)
    entry.add_to_hass(hass)
    return DHLUnifiedCoordinator(hass, client, entry), client


def _delivered(code: str, when: datetime) -> dict:
    stamp = when.isoformat()
    return body(
        shipment(
            code,
            status_code="delivered",
            status="DELIVERED",
            events=[event(stamp, "delivered", "DELIVERED")],
        )
    )


async def test_requests_are_sequential_and_spaced(hass, api_sleep):
    entry = _entry([CODE_A, CODE_B])
    coordinator, client = _coordinator(
        hass, entry, {CODE_A: body(shipment(CODE_A)), CODE_B: body(shipment(CODE_B))}
    )

    data = await coordinator._async_update_data()

    assert [c.args[0] for c in client.async_get_shipments.await_args_list] == [CODE_A, CODE_B]
    assert {p["barcode"] for p in data} == {CODE_A, CODE_B}
    # The second request waits; the first had nothing before it.
    assert api_sleep.await_count == 1
    assert 0 < api_sleep.await_args.args[0] <= DHL_UNIFIED_MIN_REQUEST_GAP_SECONDS


async def test_spacing_carries_across_polls(hass, api_sleep):
    entry = _entry([CODE_A])
    coordinator, _ = _coordinator(hass, entry, {CODE_A: body(shipment(CODE_A))})

    await coordinator._async_update_data()
    await coordinator._async_update_data()

    assert api_sleep.await_count == 1


async def test_parcel_fields_url_and_history(hass):
    entry = _entry([CODE_A], **{CONF_INCLUDE_HISTORY: True})
    coordinator, _ = _coordinator(hass, entry, {CODE_A: body(shipment(CODE_A))})

    (parcel,) = await coordinator._async_update_data()

    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["url"].endswith(f"tracking-id={CODE_A}")
    assert parcel["history"]
    assert parcel["weight"] == 2.0


async def test_rejected_key_starts_reauth(hass):
    entry = _entry([CODE_A])
    coordinator, _ = _coordinator(hass, entry, {CODE_A: DHLUnifiedKeyError("HTTP 401")})

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_not_found_shows_a_placeholder(hass):
    entry = _entry([CODE_A])
    coordinator, _ = _coordinator(hass, entry, {CODE_A: DHLUnifiedNotFound("HTTP 404")})

    (parcel,) = await coordinator._async_update_data()

    assert parcel["barcode"] == CODE_A
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["raw"] == {}


async def test_empty_shipments_list_is_a_placeholder_too(hass):
    entry = _entry([CODE_A])
    coordinator, _ = _coordinator(hass, entry, {CODE_A: {"shipments": []}})

    (parcel,) = await coordinator._async_update_data()

    assert parcel["raw"] == {}


async def test_a_failed_fetch_keeps_the_last_good_data(hass):
    entry = _entry([CODE_A, CODE_B])
    coordinator, _ = _coordinator(
        hass,
        entry,
        {
            CODE_A: [body(shipment(CODE_A)), DHLUnifiedError("HTTP 500")],
            CODE_B: body(shipment(CODE_B)),
        },
    )
    await coordinator._async_update_data()
    data = await coordinator._async_update_data()

    by_code = {p["barcode"]: p for p in data}
    assert by_code[CODE_A]["status"] == ParcelStatus.IN_TRANSIT
    assert by_code[CODE_A]["raw"]["id"] == CODE_A


async def test_every_fetch_failing_is_an_update_failure(hass):
    entry = _entry([CODE_A])
    coordinator, _ = _coordinator(hass, entry, {CODE_A: DHLUnifiedError("HTTP 500")})

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_429_stops_the_poll_and_honours_retry_after(hass):
    entry = _entry([CODE_A, CODE_B])
    coordinator, client = _coordinator(
        hass,
        entry,
        {CODE_A: DHLUnifiedRateLimitError("HTTP 429", 300), CODE_B: body(shipment(CODE_B))},
    )

    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()

    assert err.value.retry_after == 300
    assert client.async_get_shipments.await_count == 1
    assert coordinator.consecutive_rate_limits == 1


async def test_429_without_retry_after_backs_off_exponentially(hass):
    entry = _entry([CODE_A])
    coordinator, _ = _coordinator(
        hass,
        entry,
        {
            CODE_A: [
                DHLUnifiedRateLimitError("HTTP 429", None),
                DHLUnifiedRateLimitError("HTTP 429", None),
                body(shipment(CODE_A)),
            ]
        },
    )

    with pytest.raises(UpdateFailed) as first:
        await coordinator._async_update_data()
    with pytest.raises(UpdateFailed) as second:
        await coordinator._async_update_data()
    await coordinator._async_update_data()

    assert first.value.retry_after == DHL_UNIFIED_BACKOFF_BASE_SECONDS * 2
    assert second.value.retry_after == DHL_UNIFIED_BACKOFF_BASE_SECONDS * 4
    assert coordinator.consecutive_rate_limits == 0


async def test_429_keeps_what_was_fetched_before_it(hass):
    entry = _entry([CODE_A])
    coordinator, _ = _coordinator(
        hass,
        entry,
        {CODE_A: [body(shipment(CODE_A)), DHLUnifiedRateLimitError("HTTP 429", 60)]},
    )
    await coordinator._async_update_data()
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert coordinator._raw_cache[CODE_A]["id"] == CODE_A


async def test_delivered_codes_are_not_fetched_again(hass):
    now = datetime.now(timezone.utc)
    entry = _entry([CODE_A, CODE_B])
    coordinator, client = _coordinator(
        hass,
        entry,
        {CODE_A: _delivered(CODE_A, now - timedelta(days=1)), CODE_B: body(shipment(CODE_B))},
    )

    await coordinator._async_update_data()
    client.async_get_shipments.reset_mock()
    data = await coordinator._async_update_data()

    assert [c.args[0] for c in client.async_get_shipments.await_args_list] == [CODE_B]
    assert coordinator.delivered_codes == {CODE_A}
    assert [p["barcode"] for p in coordinator.delivered] == [CODE_A]
    assert [p["barcode"] for p in data] == [CODE_B]


async def test_delivered_data_is_dropped_after_30_days(hass):
    entry = _entry([CODE_A], **{CONF_DELIVERED_FILTER_TYPE: "parcels"})
    coordinator, client = _coordinator(
        hass,
        entry,
        {CODE_A: _delivered(CODE_A, datetime.now(timezone.utc) - timedelta(days=31))},
    )

    await coordinator._async_update_data()
    await coordinator._async_update_data()

    assert coordinator.delivered == []
    assert CODE_A not in coordinator._raw_cache
    assert client.async_get_shipments.await_count == 1


async def test_nothing_in_flight_stops_polling(hass):
    entry = _entry([CODE_A])
    coordinator, _ = _coordinator(
        hass, entry, {CODE_A: _delivered(CODE_A, datetime.now(timezone.utc))}
    )

    await coordinator._async_update_data()

    assert coordinator.update_interval is None


async def test_something_in_flight_keeps_polling(hass):
    entry = _entry([CODE_A])
    coordinator, _ = _coordinator(hass, entry, {CODE_A: body(shipment(CODE_A))})

    await coordinator._async_update_data()

    assert coordinator.update_interval is not None


async def test_no_codes_means_no_requests(hass):
    entry = _entry([])
    coordinator, client = _coordinator(hass, entry, {})

    assert await coordinator._async_update_data() == []
    client.async_get_shipments.assert_not_awaited()
    assert coordinator.last_success_time is not None


async def test_removed_codes_leave_the_cache(hass):
    entry = _entry([CODE_A])
    coordinator, _ = _coordinator(hass, entry, {CODE_A: body(shipment(CODE_A))})
    await coordinator._async_update_data()

    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_PARCELS: []})
    await coordinator._async_update_data()

    assert coordinator._raw_cache == {}


async def test_outgoing_codes_are_split_out(hass):
    entry = _entry(
        [CODE_A, {CONF_TRACKING_CODE: CODE_B, CONF_DIRECTION: "outgoing"}]
    )
    coordinator, _ = _coordinator(
        hass, entry, {CODE_A: body(shipment(CODE_A)), CODE_B: body(shipment(CODE_B))}
    )

    data = await coordinator._async_update_data()

    assert [p["barcode"] for p in data] == [CODE_A]
    assert [p["barcode"] for p in coordinator.outgoing] == [CODE_B]


async def test_events_fire_after_the_first_refresh(hass):
    entry = _entry(
        [CODE_A, {CONF_TRACKING_CODE: CODE_B, CONF_DIRECTION: "outgoing"}]
    )
    coordinator, _ = _coordinator(
        hass,
        entry,
        {
            CODE_A: [
                body(shipment(CODE_A, status_code="pre-transit")),
                body(
                    shipment(
                        CODE_A,
                        status_code="transit",
                        estimatedTimeOfDelivery="2026-10-01T12:00:00+02:00",
                    )
                ),
                _delivered(CODE_A, datetime.now(timezone.utc)),
            ],
            CODE_B: [
                body(shipment(CODE_B, status_code="pre-transit")),
                body(shipment(CODE_B, status_code="transit")),
                _delivered(CODE_B, datetime.now(timezone.utc)),
            ],
        },
    )
    fired: list[str] = []
    for name in (
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "parcel_delivery_time_changed",
        "outgoing_parcel_status_changed",
        "outgoing_parcel_delivered",
    ):
        hass.bus.async_listen(f"{DOMAIN}_{name}", lambda e, n=name: fired.append(n))

    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert fired == []

    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert sorted(fired) == sorted(
        ["parcel_status_changed", "parcel_delivery_time_changed", "outgoing_parcel_status_changed"]
    )

    fired.clear()
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert sorted(fired) == ["outgoing_parcel_delivered", "parcel_delivered"]


async def test_a_new_code_fires_registered(hass):
    entry = _entry([CODE_A])
    coordinator, _ = _coordinator(
        hass, entry, {CODE_A: body(shipment(CODE_A)), CODE_B: body(shipment(CODE_B))}
    )
    await coordinator._async_update_data()
    fired = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: fired.append(e.data["barcode"]))

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [{CONF_TRACKING_CODE: CODE_A}, {CONF_TRACKING_CODE: CODE_B}],
        },
    )
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == [CODE_B]


async def test_sensor_and_link_follow_the_entered_code_when_dhl_answers_another_id(hass):
    entry = _entry(["00340434292135100186"])
    coordinator, _ = _coordinator(
        hass, entry, {"00340434292135100186": body(shipment("JJD000390007000000001"))}
    )

    (parcel,) = await coordinator._async_update_data()

    assert parcel["barcode"] == "00340434292135100186"
    assert parcel["url"].endswith("tracking-id=00340434292135100186")
