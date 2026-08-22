"""Tests for the DHL coordinator: fetching, tracked-code merging, events."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dhl.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_REFRESH_TOKEN,
    CONF_TRACKED_CODES,
    DOMAIN,
    DHLApiError,
    DHLAuthError,
    ParcelStatus,
)
from custom_components.dhl.coordinator import DHLCoordinator

from .payloads import ACTIVE_CODE, active_sample, delivered_sample, in_transit_sample


def _entry(**options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="DHL (DE)",
        unique_id="DE:subject",
        data={CONF_REFRESH_TOKEN: "refresh"},
        # Keep-most-recent-100 so the delivered-retention filter never trims
        # the (old, fixed-date) sample parcels these tests assert on.
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
            CONF_TRACKED_CODES: [],
            **options,
        },
    )


def _client(elements=None, rate_limited: bool = False) -> AsyncMock:
    client = AsyncMock()
    client.async_get_incoming.return_value = (elements or [], rate_limited)
    client.async_get_by_number.return_value = None
    return client


def _coordinator(hass, entry, client) -> DHLCoordinator:
    de_session = MagicMock()
    de_session.pop_refresh_token_changed.return_value = False
    return DHLCoordinator(hass, client, entry, de_session=de_session)


# ---------------------------------------------------------------------------
# fetching + tracked-code merging
# ---------------------------------------------------------------------------


async def test_update_splits_active_and_delivered(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client([active_sample(), delivered_sample()])
    coordinator = _coordinator(hass, entry, client)

    data = await coordinator._async_update_data()

    assert [parcel["barcode"] for parcel in data] == [ACTIVE_CODE]
    assert len(coordinator.delivered) == 1
    assert coordinator.last_success_time is not None


async def test_update_handles_an_empty_inbox(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry, _client([]))

    assert await coordinator._async_update_data() == []


async def test_expired_session_triggers_reauth(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_incoming.side_effect = DHLAuthError("session expired")
    coordinator = _coordinator(hass, entry, client)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_tracked_code_missing_from_inbox_is_fetched_by_number(hass):
    entry = _entry(**{CONF_TRACKED_CODES: ["MANUAL0000001"]})
    entry.add_to_hass(hass)
    client = _client([])
    client.async_get_by_number.return_value = active_sample("MANUAL0000001")
    coordinator = _coordinator(hass, entry, client)

    data = await coordinator._async_update_data()

    client.async_get_by_number.assert_awaited_once_with("MANUAL0000001")
    assert [p["barcode"] for p in data] == ["MANUAL0000001"]


async def test_tracked_code_already_in_inbox_is_not_fetched_again(hass):
    entry = _entry(**{CONF_TRACKED_CODES: [ACTIVE_CODE]})
    entry.add_to_hass(hass)
    client = _client([active_sample()])
    coordinator = _coordinator(hass, entry, client)

    await coordinator._async_update_data()

    client.async_get_by_number.assert_not_called()


async def test_tracked_code_fetch_failure_is_logged_and_skipped(hass):
    entry = _entry(**{CONF_TRACKED_CODES: ["MANUAL0000001"]})
    entry.add_to_hass(hass)
    client = _client([])
    client.async_get_by_number.side_effect = DHLApiError("boom")
    coordinator = _coordinator(hass, entry, client)

    data = await coordinator._async_update_data()

    assert data == []


async def test_tracked_code_auth_error_triggers_reauth(hass):
    entry = _entry(**{CONF_TRACKED_CODES: ["MANUAL0000001"]})
    entry.add_to_hass(hass)
    client = _client([])
    client.async_get_by_number.side_effect = DHLAuthError("expired")
    coordinator = _coordinator(hass, entry, client)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# rateLimited flip
# ---------------------------------------------------------------------------


async def test_rate_limited_flip_warns_once(hass, caplog):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client([], rate_limited=True)
    coordinator = _coordinator(hass, entry, client)

    await coordinator._async_update_data()
    caplog.clear()
    await coordinator._async_update_data()  # stays True — no repeat warning

    assert "ratelimited" not in caplog.text.lower()


async def test_rate_limited_false_to_true_warns(hass, caplog):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client([], rate_limited=False)
    coordinator = _coordinator(hass, entry, client)
    await coordinator._async_update_data()

    client.async_get_incoming.return_value = ([], True)
    await coordinator._async_update_data()

    assert "ratelimited" in caplog.text.lower()


# ---------------------------------------------------------------------------
# out-for-delivery stall warning
# ---------------------------------------------------------------------------


async def test_stalled_out_for_delivery_warns_on_second_poll(hass, caplog):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client([active_sample()])  # fortschritt=4 -> OUT_FOR_DELIVERY
    coordinator = _coordinator(hass, entry, client)

    await coordinator._async_update_data()
    assert "stayed" not in caplog.text.lower()
    await coordinator._async_update_data()

    assert "stayed" in caplog.text.lower()


async def test_out_for_delivery_streak_resets_on_status_change(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client([active_sample()])
    coordinator = _coordinator(hass, entry, client)
    await coordinator._async_update_data()

    client.async_get_incoming.return_value = ([delivered_sample(ACTIVE_CODE)], False)
    await coordinator._async_update_data()

    assert ACTIVE_CODE not in coordinator._out_for_delivery_streak


# ---------------------------------------------------------------------------
# refresh-token persistence
# ---------------------------------------------------------------------------


async def test_rotated_refresh_token_is_persisted(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client([])
    de_session = MagicMock()
    de_session.pop_refresh_token_changed.return_value = True
    de_session.refresh_token = "new-refresh-token"
    coordinator = DHLCoordinator(hass, client, entry, de_session=de_session)

    await coordinator._async_update_data()

    assert entry.data[CONF_REFRESH_TOKEN] == "new-refresh-token"


# ---------------------------------------------------------------------------
# events (mirrors the other suite carriers)
# ---------------------------------------------------------------------------


async def test_first_refresh_fires_nothing(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry, _client([active_sample()]))

    fired = []
    for suffix in (
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "parcel_delivery_time_changed",
    ):
        hass.bus.async_listen(f"{DOMAIN}_{suffix}", lambda e: fired.append(e))

    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_fires_status_changed_event(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client([in_transit_sample()])
    coordinator = _coordinator(hass, entry, client)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    await coordinator._async_update_data()  # first refresh: suppressed
    client.async_get_incoming.return_value = ([active_sample()], False)
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["old_status"] == ParcelStatus.IN_TRANSIT
    assert events[0].data["new_status"] == ParcelStatus.OUT_FOR_DELIVERY


async def test_delivery_fires_delivered_event_and_not_status_changed(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client([active_sample()])
    coordinator = _coordinator(hass, entry, client)

    delivered = []
    changed = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: changed.append(e)
    )

    await coordinator._async_update_data()
    client.async_get_incoming.return_value = ([delivered_sample(ACTIVE_CODE)], False)
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert changed == []
    assert len(delivered) == 1
    assert delivered[0].data["status"] == ParcelStatus.DELIVERED


async def test_fires_registered_event_for_new_parcel(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client([active_sample()])
    coordinator = _coordinator(hass, entry, client)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))

    await coordinator._async_update_data()  # first refresh: suppressed
    client.async_get_incoming.return_value = (
        [active_sample(), active_sample("SECOND000001")],
        False,
    )
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["barcode"] == "SECOND000001"


async def test_fires_delivery_time_changed_event(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client([active_sample()])
    coordinator = _coordinator(hass, entry, client)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: events.append(e)
    )

    await coordinator._async_update_data()  # first refresh: suppressed

    moved = active_sample()
    moved["sendungsdetails"]["zustellung"] = {
        "zustellzeitfensterVon": "2026-04-29T16:00:00+02:00",
        "zustellzeitfensterBis": "2026-04-29T18:00:00+02:00",
    }
    client.async_get_incoming.return_value = ([moved], False)
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["new_planned_from"] == "2026-04-29T14:00:00+00:00"


async def test_event_carries_device_id(hass):
    from homeassistant.helpers import device_registry as dr

    entry = _entry()
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
    )
    client = _client([in_transit_sample()])
    coordinator = _coordinator(hass, entry, client)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    await coordinator._async_update_data()
    client.async_get_incoming.return_value = ([active_sample()], False)
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events[0].data["device_id"] == device.id
