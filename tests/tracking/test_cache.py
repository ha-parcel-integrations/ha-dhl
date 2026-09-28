"""Tests for the tracking coordinator's persisted cache."""
import time
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.dhl.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_TRACKING,
    TRACKING_STORAGE_KEY,
    ParcelStatus,
)
from custom_components.dhl.tracking.coordinator import (
    DHLTrackingCoordinator,
    _stagger_minutes,
)

from .payloads import express_in_transit

EXPRESS_CODE = "1000000002"


def _entry(codes: list[str]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="DHL tracking",
        unique_id=SOURCE_TRACKING,
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
            CONF_PARCELS: [{CONF_TRACKING_CODE: c} for c in codes],
        },
    )


def _key(entry: MockConfigEntry) -> str:
    return f"{TRACKING_STORAGE_KEY}.{entry.entry_id}"


async def _flush(hass) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()


async def test_a_restart_keeps_the_spent_budget_and_the_last_express_payload(
    hass, hass_storage
):
    entry = _entry([EXPRESS_CODE])
    entry.add_to_hass(hass)
    first = DHLTrackingCoordinator(hass, AsyncMock(), entry)
    await first.async_load_cache()

    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_express",
        AsyncMock(return_value=express_in_transit(EXPRESS_CODE)),
    ):
        await first._async_update_data()
    await _flush(hass)
    assert _key(entry) in hass_storage

    second = DHLTrackingCoordinator(hass, AsyncMock(), entry)
    await second.async_load_cache()
    assert second.express_budget_available == 0

    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_express"
    ) as fetch_express:
        data = await second._async_update_data()

    fetch_express.assert_not_called()
    assert data[0]["barcode"] == EXPRESS_CODE
    assert data[0]["status"] == ParcelStatus.IN_TRANSIT


async def test_a_restored_standdown_is_still_waited_out(hass, hass_storage):
    entry = _entry([EXPRESS_CODE])
    entry.add_to_hass(hass)
    deadline = time.time() + 3600
    hass_storage[_key(entry)] = {
        "version": 1,
        "key": _key(entry),
        "data": {"consecutive_failures": 2, "standdown_until_utc": deadline},
    }

    coordinator = DHLTrackingCoordinator(hass, AsyncMock(), entry)
    await coordinator.async_load_cache()

    assert coordinator.express_standing_down is True
    assert coordinator.express_consecutive_failures == 2
    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_express"
    ) as fetch_express:
        await coordinator._async_update_data()
    fetch_express.assert_not_called()
    assert coordinator.update_interval.total_seconds() >= 3500


async def test_a_malformed_store_starts_cold(hass, hass_storage):
    entry = _entry([EXPRESS_CODE])
    entry.add_to_hass(hass)
    hass_storage[_key(entry)] = {
        "version": 1,
        "key": _key(entry),
        "data": {
            "raw_cache": {EXPRESS_CODE: {"id": EXPRESS_CODE}, 5: {}},
            "delivered_codes": "not-a-list",
            "attempted_codes": [EXPRESS_CODE, 7],
            "last_fetch_by_code": {EXPRESS_CODE: "yesterday"},
            "consecutive_failures": -1,
            "standdown_until_utc": "soon",
            "budget": {"tokens": -3, "updated_utc": 1},
        },
    }

    coordinator = DHLTrackingCoordinator(hass, AsyncMock(), entry)
    await coordinator.async_load_cache()

    # A cached payload without its backend marker cannot be normalized.
    assert coordinator._raw_cache == {}
    assert coordinator._delivered_codes == set()
    assert coordinator._attempted_codes == {EXPRESS_CODE}
    assert coordinator._last_fetch_by_code == {}
    assert coordinator.express_consecutive_failures == 0
    assert coordinator.express_standing_down is False
    assert coordinator.express_budget_available == 1


async def test_no_store_at_all_starts_cold(hass):
    entry = _entry([EXPRESS_CODE])
    entry.add_to_hass(hass)
    coordinator = DHLTrackingCoordinator(hass, AsyncMock(), entry)

    await coordinator.async_load_cache()

    assert coordinator.express_budget_available == 1
    assert coordinator._raw_cache == {}


async def test_remove_cache_deletes_the_store(hass, hass_storage):
    entry = _entry([EXPRESS_CODE])
    entry.add_to_hass(hass)
    coordinator = DHLTrackingCoordinator(hass, AsyncMock(), entry)
    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_express",
        AsyncMock(return_value=express_in_transit(EXPRESS_CODE)),
    ):
        await coordinator._async_update_data()
    await _flush(hass)
    assert _key(entry) in hass_storage

    await coordinator.async_remove_cache()

    assert _key(entry) not in hass_storage


def test_stagger_is_stable_for_an_entry():
    assert _stagger_minutes("abc") == _stagger_minutes("abc")
