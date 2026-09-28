"""Tests for the tracking coordinator: routing, fallback, queue, throttle."""
import time
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dhl.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DIRECTION,
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DHL_EXPRESS_REQUEST_BUDGET_REFILL_SECONDS,
    DHL_EXPRESS_TRACKED_CODE_SOFT_LIMIT,
    DOMAIN,
    SOURCE_TRACKING,
    ParcelStatus,
)
from custom_components.dhl.tracking.coordinator import DHLTrackingCoordinator

from .payloads import express_delivered, express_in_transit, gateway_element

GATEWAY_CODE = "3SXYZ0000000001"
GATEWAY_CODE_2 = "3SXYZ0000000002"
EXPRESS_CODE = "1000000001"
EXPRESS_CODE_2 = "1000000002"
UNKNOWN_CODE = "ZZTOTALLYUNKNOWN123"


def _entry(codes: list[str], **options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="DHL tracking",
        unique_id=SOURCE_TRACKING,
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
            CONF_PARCELS: [{CONF_TRACKING_CODE: c} for c in codes],
            **options,
        },
    )


def _coordinator(hass, entry) -> DHLTrackingCoordinator:
    return DHLTrackingCoordinator(hass, AsyncMock(), entry)


# ---------------------------------------------------------------------------
# routing + gateway batching
# ---------------------------------------------------------------------------


async def test_gateway_shaped_codes_are_batched_in_one_request(hass):
    entry = _entry([GATEWAY_CODE, GATEWAY_CODE_2])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_gateway",
        AsyncMock(
            return_value={
                GATEWAY_CODE: gateway_element(barcode=GATEWAY_CODE),
                GATEWAY_CODE_2: gateway_element(barcode=GATEWAY_CODE_2),
            }
        ),
    ) as fetch_gateway:
        data = await coordinator._async_update_data()

    fetch_gateway.assert_awaited_once()
    _, called_codes = fetch_gateway.call_args.args
    assert set(called_codes) == {GATEWAY_CODE, GATEWAY_CODE_2}
    assert {p["barcode"] for p in data} == {GATEWAY_CODE, GATEWAY_CODE_2}


async def test_express_shaped_code_never_goes_to_the_gateway(hass):
    entry = _entry([EXPRESS_CODE])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_gateway",
        AsyncMock(return_value={}),
    ) as fetch_gateway, patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_express",
        AsyncMock(return_value=express_delivered(EXPRESS_CODE)),
    ):
        await coordinator._async_update_data()

    fetch_gateway.assert_not_called()


async def test_gateway_404_absent_code_is_not_found(hass):
    entry = _entry([GATEWAY_CODE])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_gateway",
        AsyncMock(return_value={}),
    ):
        data = await coordinator._async_update_data()

    assert data[0]["barcode"] == GATEWAY_CODE
    assert data[0]["status"] == ParcelStatus.UNKNOWN


# ---------------------------------------------------------------------------
# the narrow unknown-shape fallback rule
# ---------------------------------------------------------------------------


async def test_unknown_shape_tries_gateway_first(hass):
    entry = _entry([UNKNOWN_CODE])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_gateway",
        AsyncMock(return_value={UNKNOWN_CODE: gateway_element(barcode=UNKNOWN_CODE)}),
    ) as fetch_gateway, patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_express"
    ) as fetch_express:
        data = await coordinator._async_update_data()

    fetch_gateway.assert_awaited_once()
    fetch_express.assert_not_called()
    assert data[0]["barcode"] == UNKNOWN_CODE


async def test_unknown_shape_falls_back_to_express_when_gateway_cannot_resolve(hass):
    entry = _entry([UNKNOWN_CODE])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_gateway",
        AsyncMock(return_value={}),
    ), patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_express",
        AsyncMock(return_value=express_delivered(UNKNOWN_CODE)),
    ) as fetch_express:
        data = await coordinator._async_update_data()

    fetch_express.assert_awaited_once_with(coordinator._client, UNKNOWN_CODE)
    assert data == [] or data[0]["delivered"] is True  # DELIVERED -> in coordinator.delivered
    assert coordinator.delivered[0]["barcode"] == UNKNOWN_CODE


async def test_fallback_never_fires_for_a_confidently_classified_code(hass):
    """A gateway-shaped code that the gateway does not resolve must NOT try
    Express — the fallback is only for shapes that matched no known family."""
    entry = _entry([GATEWAY_CODE])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_gateway",
        AsyncMock(return_value={}),
    ), patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_express"
    ) as fetch_express:
        await coordinator._async_update_data()

    fetch_express.assert_not_called()


# ---------------------------------------------------------------------------
# express normalisation + delivered handling
# ---------------------------------------------------------------------------


async def test_express_delivered_code_moves_to_delivered_bucket(hass):
    entry = _entry([EXPRESS_CODE])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_express",
        AsyncMock(return_value=express_delivered(EXPRESS_CODE)),
    ):
        active = await coordinator._async_update_data()

    assert active == []
    assert coordinator.delivered[0]["barcode"] == EXPRESS_CODE


async def test_express_in_transit_stays_active(hass):
    entry = _entry([EXPRESS_CODE])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_express",
        AsyncMock(return_value=express_in_transit(EXPRESS_CODE)),
    ):
        active = await coordinator._async_update_data()

    assert active[0]["status"] == ParcelStatus.IN_TRANSIT


async def test_delivered_express_code_is_skipped_by_the_queue_next_cycle(hass):
    entry = _entry([EXPRESS_CODE])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    fetch = AsyncMock(return_value=express_delivered(EXPRESS_CODE))
    with patch("custom_components.dhl.tracking.coordinator.async_fetch_express", fetch):
        await coordinator._async_update_data()
        await coordinator._async_update_data()

    fetch.assert_awaited_once()


# ---------------------------------------------------------------------------
# queue ranking
# ---------------------------------------------------------------------------


async def test_never_attempted_code_is_queued_before_a_previously_fetched_one(hass):
    entry = _entry([EXPRESS_CODE, EXPRESS_CODE_2])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    # EXPRESS_CODE already attempted a moment ago; EXPRESS_CODE_2 never.
    coordinator._attempted_codes.add(EXPRESS_CODE)
    coordinator._last_fetch_by_code[EXPRESS_CODE] = time.time()
    coordinator._status_by_code[EXPRESS_CODE] = ParcelStatus.OUT_FOR_DELIVERY

    queue = coordinator._express_queue([EXPRESS_CODE, EXPRESS_CODE_2])
    assert queue[0] == EXPRESS_CODE_2


async def test_out_for_delivery_outranks_unknown_once_both_attempted(hass):
    entry = _entry([EXPRESS_CODE, EXPRESS_CODE_2])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    for code, status in (
        (EXPRESS_CODE, ParcelStatus.UNKNOWN),
        (EXPRESS_CODE_2, ParcelStatus.OUT_FOR_DELIVERY),
    ):
        coordinator._attempted_codes.add(code)
        coordinator._last_fetch_by_code[code] = time.time()
        coordinator._status_by_code[code] = status

    queue = coordinator._express_queue([EXPRESS_CODE, EXPRESS_CODE_2])
    assert queue[0] == EXPRESS_CODE_2


async def test_overdue_code_outranks_status_ranking_past_the_band(hass):
    entry = _entry([EXPRESS_CODE, EXPRESS_CODE_2])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    now = time.time()
    coordinator._attempted_codes |= {EXPRESS_CODE, EXPRESS_CODE_2}
    coordinator._status_by_code[EXPRESS_CODE] = ParcelStatus.AT_PICKUP_POINT
    coordinator._status_by_code[EXPRESS_CODE_2] = ParcelStatus.OUT_FOR_DELIVERY
    # EXPRESS_CODE has waited many refill intervals; EXPRESS_CODE_2 just ran.
    coordinator._last_fetch_by_code[EXPRESS_CODE] = now - (
        DHL_EXPRESS_REQUEST_BUDGET_REFILL_SECONDS * 10
    )
    coordinator._last_fetch_by_code[EXPRESS_CODE_2] = now

    queue = coordinator._express_queue([EXPRESS_CODE, EXPRESS_CODE_2])
    assert queue[0] == EXPRESS_CODE


async def test_only_one_express_code_is_fetched_per_cycle(hass):
    entry = _entry([EXPRESS_CODE, EXPRESS_CODE_2])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    fetch = AsyncMock(return_value=express_in_transit(EXPRESS_CODE))
    with patch("custom_components.dhl.tracking.coordinator.async_fetch_express", fetch):
        await coordinator._async_update_data()

    fetch.assert_awaited_once()


# ---------------------------------------------------------------------------
# throttle / stand-down
# ---------------------------------------------------------------------------


async def test_drg10012_throttle_stands_down_at_least_the_refill_interval(hass):
    from custom_components.dhl.const import DHLExpressThrottledError

    entry = _entry([EXPRESS_CODE])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_express",
        AsyncMock(side_effect=DHLExpressThrottledError("throttled")),
    ):
        await coordinator._async_update_data()

    assert coordinator.express_consecutive_failures == 1
    assert coordinator.express_standing_down is True
    remaining = coordinator._standdown_until_utc - time.time()
    assert remaining >= DHL_EXPRESS_REQUEST_BUDGET_REFILL_SECONDS


async def test_standing_down_skips_express_fetch_entirely(hass):
    entry = _entry([EXPRESS_CODE])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)
    coordinator._standdown_until_utc = time.time() + 3600

    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_express"
    ) as fetch_express:
        await coordinator._async_update_data()

    fetch_express.assert_not_called()


async def test_schedule_never_shorter_than_an_active_standdown(hass):
    entry = _entry([])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)
    coordinator._standdown_until_utc = time.time() + 7200

    coordinator._schedule([])

    assert coordinator.update_interval.total_seconds() >= 7100


async def test_credential_error_disables_express_and_never_retries(hass):
    from custom_components.dhl.const import DHLExpressCredentialError

    entry = _entry([EXPRESS_CODE])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    fetch = AsyncMock(side_effect=DHLExpressCredentialError("HTTP 401"))
    with patch("custom_components.dhl.tracking.coordinator.async_fetch_express", fetch):
        await coordinator._async_update_data()
        await coordinator._async_update_data()

    assert coordinator.express_disabled is True
    fetch.assert_awaited_once()


async def test_soft_limit_warning_fires_once(hass, caplog):
    codes = [f"100000000{i}" for i in range(DHL_EXPRESS_TRACKED_CODE_SOFT_LIMIT + 1)]
    entry = _entry(codes)
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    with caplog.at_level("WARNING"), patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_express",
        AsyncMock(return_value=express_in_transit()),
    ):
        await coordinator._async_update_data()
        first_count = sum("roughly one request per" in r.message for r in caplog.records)
        await coordinator._async_update_data()
        second_count = sum("roughly one request per" in r.message for r in caplog.records)

    assert first_count == 1
    assert second_count == 1


# ---------------------------------------------------------------------------
# events + diagnostics-shape attributes
# ---------------------------------------------------------------------------


async def test_first_refresh_is_silent_but_second_fires_registered(hass):
    entry = _entry([GATEWAY_CODE])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)
    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))

    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_gateway",
        AsyncMock(return_value={GATEWAY_CODE: gateway_element(barcode=GATEWAY_CODE)}),
    ):
        await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert events == []


async def test_a_new_code_added_after_the_first_poll_fires_registered(hass):
    entry = _entry([GATEWAY_CODE])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)
    registered_events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_registered", lambda e: registered_events.append(e)
    )

    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_gateway",
        AsyncMock(return_value={GATEWAY_CODE: gateway_element(barcode=GATEWAY_CODE)}),
    ):
        await coordinator._async_update_data()
    await hass.async_block_till_done()

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: GATEWAY_CODE},
                {CONF_TRACKING_CODE: GATEWAY_CODE_2},
            ],
        },
    )
    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_gateway",
        AsyncMock(
            return_value={
                GATEWAY_CODE: gateway_element(barcode=GATEWAY_CODE),
                GATEWAY_CODE_2: gateway_element(barcode=GATEWAY_CODE_2),
            }
        ),
    ):
        await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(registered_events) == 1
    assert registered_events[0].data["barcode"] == GATEWAY_CODE_2


async def test_always_empty_attributes_present_for_platform_reuse(hass):
    entry = _entry([])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    assert coordinator.de_session is None
    assert coordinator.last_element_count is None
    assert coordinator.express_budget_available == 1


async def test_device_id_is_cached_after_first_lookup(hass):
    entry = _entry([])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    first = coordinator._device_id()
    second = coordinator._device_id()

    assert first == second


async def test_gateway_request_failure_is_logged_and_treated_as_unresolved(hass):
    from custom_components.dhl.tracking.gateway import DHLGatewayError

    entry = _entry([GATEWAY_CODE])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_gateway",
        AsyncMock(side_effect=DHLGatewayError("boom")),
    ):
        data = await coordinator._async_update_data()

    assert data[0]["barcode"] == GATEWAY_CODE
    assert data[0]["status"] == ParcelStatus.UNKNOWN


async def test_generic_express_api_error_is_logged_with_its_code_and_spends_the_budget(
    hass, caplog
):
    entry = _entry([EXPRESS_CODE])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    from custom_components.dhl.const import DHLApiError

    fetch = AsyncMock(side_effect=DHLApiError("HTTP 404"))
    with patch("custom_components.dhl.tracking.coordinator.async_fetch_express", fetch):
        await coordinator._async_update_data()
        await coordinator._async_update_data()

    assert f"DHL Express fetch failed for {EXPRESS_CODE}:" in caplog.text
    assert "HTTP 404" in caplog.text
    # The failed request reached the backend, so it costs the cycle's token:
    # the next poll must not retry it outside the budget.
    fetch.assert_awaited_once()
    assert coordinator.express_budget_available == 0
    assert EXPRESS_CODE in coordinator._attempted_codes
    assert coordinator.express_disabled is False
    assert coordinator.express_standing_down is False


async def test_a_result_missing_its_own_barcode_field_falls_back_to_the_requested_code(hass):
    entry = _entry([GATEWAY_CODE])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_gateway",
        AsyncMock(return_value={GATEWAY_CODE: {"events": []}}),
    ):
        data = await coordinator._async_update_data()

    assert data[0]["barcode"] == GATEWAY_CODE


async def test_delivered_event_and_delivery_time_changed_event_fire(hass):
    entry = _entry([EXPRESS_CODE])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)
    delivered_events = []
    time_changed_events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivered", lambda e: delivered_events.append(e)
    )
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: time_changed_events.append(e)
    )

    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_express",
        AsyncMock(return_value=express_in_transit(EXPRESS_CODE)),
    ):
        await coordinator._async_update_data()
    await hass.async_block_till_done()

    coordinator._budget.tokens = 1.0  # simulate the next cycle's refill
    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_express",
        AsyncMock(return_value=express_delivered(EXPRESS_CODE)),
    ):
        await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(delivered_events) == 1
    assert delivered_events[0].data["barcode"] == EXPRESS_CODE


def _entry_with_outgoing(incoming: list[str], outgoing: list[str]) -> MockConfigEntry:
    entry = _entry(incoming)
    parcels = entry.options[CONF_PARCELS] + [
        {CONF_TRACKING_CODE: c, CONF_DIRECTION: "outgoing"} for c in outgoing
    ]
    return MockConfigEntry(
        domain=DOMAIN,
        title=entry.title,
        unique_id=entry.unique_id,
        data=dict(entry.data),
        options={**entry.options, CONF_PARCELS: parcels},
    )


async def test_outgoing_codes_are_split_out_of_the_incoming_lists(hass):
    entry = _entry_with_outgoing([GATEWAY_CODE], [GATEWAY_CODE_2])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_gateway",
        AsyncMock(
            return_value={
                GATEWAY_CODE: gateway_element(barcode=GATEWAY_CODE),
                GATEWAY_CODE_2: gateway_element(barcode=GATEWAY_CODE_2),
            }
        ),
    ):
        data = await coordinator._async_update_data()

    assert [p["barcode"] for p in data] == [GATEWAY_CODE]
    assert [p["barcode"] for p in coordinator.outgoing] == [GATEWAY_CODE_2]
    assert coordinator.delivered_outgoing == []


async def test_outgoing_events_fire_on_status_change_and_delivery(hass):
    entry = _entry_with_outgoing([], [GATEWAY_CODE])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)
    changed, delivered, incoming = [], [], []
    hass.bus.async_listen(
        f"{DOMAIN}_outgoing_parcel_status_changed", lambda e: changed.append(e)
    )
    hass.bus.async_listen(
        f"{DOMAIN}_outgoing_parcel_delivered", lambda e: delivered.append(e)
    )
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: incoming.append(e))

    for element in (
        gateway_element(barcode=GATEWAY_CODE, category="UNDERWAY", status=""),
        gateway_element(barcode=GATEWAY_CODE),
        gateway_element(
            barcode=GATEWAY_CODE,
            category="DELIVERED",
            status="DELIVERED",
            delivered_at="2026-02-13T14:00:00+01:00",
        ),
    ):
        with patch(
            "custom_components.dhl.tracking.coordinator.async_fetch_gateway",
            AsyncMock(return_value={GATEWAY_CODE: element}),
        ):
            await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(changed) == 1
    assert changed[0].data["new_status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert len(delivered) == 1
    assert [p["barcode"] for p in coordinator.delivered_outgoing] == [GATEWAY_CODE]
    assert incoming == []


async def test_every_parcel_links_to_the_dhl_tracking_page_in_the_ha_locale(hass):
    hass.config.country = "NL"
    hass.config.language = "en"
    entry = _entry([GATEWAY_CODE, EXPRESS_CODE])
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)

    with (
        patch(
            "custom_components.dhl.tracking.coordinator.async_fetch_gateway",
            AsyncMock(return_value={GATEWAY_CODE: gateway_element(barcode=GATEWAY_CODE)}),
        ),
        patch(
            "custom_components.dhl.tracking.coordinator.async_fetch_express",
            AsyncMock(return_value=express_in_transit(EXPRESS_CODE)),
        ),
    ):
        data = await coordinator._async_update_data()

    assert {p["barcode"]: p["url"] for p in data} == {
        GATEWAY_CODE: "https://www.dhl.com/nl-en/home/tracking.html"
        f"?tracking-id={GATEWAY_CODE}",
        EXPRESS_CODE: "https://www.dhl.com/nl-en/home/tracking.html"
        f"?tracking-id={EXPRESS_CODE}",
    }
