"""Tests for the DHL track_parcel / untrack_parcel services."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dhl.const import (
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKED_CODES,
    DOMAIN,
    SOURCE_API,
    SOURCE_TRACKING,
)
from custom_components.dhl.services import (
    async_setup_services,
    async_unload_services,
    normalize_tracking_code,
    valid_tracking_code,
)


def _entry(entry_id: str = "e1", **options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        entry_id=entry_id,
        title="Germany",
        unique_id=f"DE:{entry_id}",
        data={},
        options={CONF_TRACKED_CODES: [], **options},
    )


def _code_based_entry(
    entry_id: str = "t1", source: str = SOURCE_TRACKING, parcels=None
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        entry_id=entry_id,
        title="DHL tracking",
        unique_id=f"{source}:{entry_id}",
        data={CONF_SOURCE: source},
        options={CONF_PARCELS: list(parcels or [])},
    )


def test_normalize_tracking_code_strips_whitespace():
    assert normalize_tracking_code("  ABC123  \n") == "ABC123"


@pytest.mark.parametrize(
    "code,expected",
    [
        ("00340434161094681228", True),
        ("JVGL06258198000117303", True),
        ("short", False),
        ("way-too-long-" + "x" * 30, False),
        ("has spaces here", False),
    ],
)
def test_valid_tracking_code(code, expected):
    assert valid_tracking_code(code) is expected


async def test_track_parcel_adds_code(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    async_setup_services(hass)

    await hass.services.async_call(
        DOMAIN,
        "track_parcel",
        {"tracking_code": "00340434161094681228"},
        blocking=True,
    )

    assert entry.options[CONF_TRACKED_CODES] == ["00340434161094681228"]


async def test_track_parcel_is_idempotent(hass):
    entry = _entry(**{CONF_TRACKED_CODES: ["00340434161094681228"]})
    entry.add_to_hass(hass)
    async_setup_services(hass)

    await hass.services.async_call(
        DOMAIN,
        "track_parcel",
        {"tracking_code": "00340434161094681228"},
        blocking=True,
    )

    assert entry.options[CONF_TRACKED_CODES] == ["00340434161094681228"]


async def test_track_parcel_rejects_invalid_code(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    async_setup_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {"tracking_code": "!!"}, blocking=True
        )


async def test_untrack_parcel_removes_code(hass):
    entry = _entry(**{CONF_TRACKED_CODES: ["00340434161094681228", "OTHER00001"]})
    entry.add_to_hass(hass)
    async_setup_services(hass)

    await hass.services.async_call(
        DOMAIN,
        "untrack_parcel",
        {"tracking_code": "00340434161094681228"},
        blocking=True,
    )

    assert entry.options[CONF_TRACKED_CODES] == ["OTHER00001"]


async def test_untrack_parcel_no_op_when_not_tracked(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    async_setup_services(hass)

    await hass.services.async_call(
        DOMAIN, "untrack_parcel", {"tracking_code": "NOTTRACKED1"}, blocking=True
    )

    assert entry.options[CONF_TRACKED_CODES] == []


async def test_no_entries_raises(hass):
    async_setup_services(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {"tracking_code": "00340434161094681228"},
            blocking=True,
        )


async def test_ambiguous_entries_require_config_entry_id(hass):
    _entry("e1").add_to_hass(hass)
    _entry("e2").add_to_hass(hass)
    async_setup_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {"tracking_code": "00340434161094681228"},
            blocking=True,
        )


async def test_ambiguous_code_based_entries_require_config_entry_id(hass):
    _code_based_entry("t1").add_to_hass(hass)
    _code_based_entry("a1", source=SOURCE_API).add_to_hass(hass)
    async_setup_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {"tracking_code": "31570000000"}, blocking=True
        )


async def test_lone_account_stays_the_default_next_to_a_tracking_entry(hass):
    account = _entry("e1")
    account.add_to_hass(hass)
    tracking = _code_based_entry("t1")
    tracking.add_to_hass(hass)
    async_setup_services(hass)

    await hass.services.async_call(
        DOMAIN,
        "track_parcel",
        {"tracking_code": "00340434161094681228"},
        blocking=True,
    )

    assert account.options[CONF_TRACKED_CODES] == ["00340434161094681228"]
    assert tracking.options[CONF_PARCELS] == []


async def test_track_parcel_adds_a_digit_only_code_to_a_tracking_entry(hass):
    entry = _code_based_entry()
    entry.add_to_hass(hass)
    async_setup_services(hass)

    await hass.services.async_call(
        DOMAIN, "track_parcel", {"tracking_code": " 31570000000 "}, blocking=True
    )

    assert entry.options[CONF_PARCELS] == [
        {"tracking_code": "31570000000", "direction": "incoming"}
    ]


async def test_track_parcel_targets_an_api_entry_by_config_entry_id(hass):
    _entry("e1").add_to_hass(hass)
    api = _code_based_entry("a1", source=SOURCE_API)
    api.add_to_hass(hass)
    async_setup_services(hass)

    await hass.services.async_call(
        DOMAIN,
        "track_parcel",
        {"tracking_code": "jd014600006000000000", "direction": "outgoing",
         "config_entry_id": "a1"},
        blocking=True,
    )

    assert api.options[CONF_PARCELS] == [
        {"tracking_code": "JD014600006000000000", "direction": "outgoing"}
    ]


async def test_track_parcel_on_a_tracking_entry_is_idempotent(hass):
    parcels = [{"tracking_code": "31570000000", "direction": "incoming"}]
    entry = _code_based_entry(parcels=parcels)
    entry.add_to_hass(hass)
    async_setup_services(hass)

    await hass.services.async_call(
        DOMAIN, "track_parcel", {"tracking_code": "31570000000"}, blocking=True
    )

    assert entry.options[CONF_PARCELS] == parcels


async def test_track_parcel_with_the_other_direction_moves_it(hass):
    entry = _code_based_entry(parcels=[{"tracking_code": "31570000000"}])
    entry.add_to_hass(hass)
    async_setup_services(hass)

    await hass.services.async_call(
        DOMAIN,
        "track_parcel",
        {"tracking_code": "31570000000", "direction": "outgoing"},
        blocking=True,
    )

    assert entry.options[CONF_PARCELS] == [
        {"tracking_code": "31570000000", "direction": "outgoing"}
    ]


async def test_track_parcel_rejects_an_empty_code_on_a_tracking_entry(hass):
    _code_based_entry().add_to_hass(hass)
    async_setup_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {"tracking_code": "   "}, blocking=True
        )


async def test_untrack_parcel_removes_a_code_from_a_tracking_entry(hass):
    entry = _code_based_entry(
        parcels=[
            {"tracking_code": "31570000000", "direction": "incoming"},
            {"tracking_code": "3SXYZ0000000001", "direction": "outgoing"},
        ]
    )
    entry.add_to_hass(hass)
    async_setup_services(hass)

    await hass.services.async_call(
        DOMAIN, "untrack_parcel", {"tracking_code": "3sxyz0000000001"}, blocking=True
    )
    await hass.services.async_call(
        DOMAIN, "untrack_parcel", {"tracking_code": "NOTTRACKED1"}, blocking=True
    )

    assert entry.options[CONF_PARCELS] == [
        {"tracking_code": "31570000000", "direction": "incoming"}
    ]


async def test_config_entry_id_disambiguates(hass):
    entry1 = _entry("e1")
    entry1.add_to_hass(hass)
    entry2 = _entry("e2")
    entry2.add_to_hass(hass)
    async_setup_services(hass)

    await hass.services.async_call(
        DOMAIN,
        "track_parcel",
        {"tracking_code": "00340434161094681228", "config_entry_id": "e2"},
        blocking=True,
    )

    assert entry1.options[CONF_TRACKED_CODES] == []
    assert entry2.options[CONF_TRACKED_CODES] == ["00340434161094681228"]


async def test_unknown_config_entry_id_raises(hass):
    _entry("e1").add_to_hass(hass)
    async_setup_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {"tracking_code": "00340434161094681228", "config_entry_id": "nope"},
            blocking=True,
        )


async def test_setup_services_is_idempotent(hass):
    async_setup_services(hass)
    async_setup_services(hass)
    assert hass.services.has_service(DOMAIN, "track_parcel")


async def test_track_parcel_nudges_a_loaded_entry(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = MagicMock()
    coordinator.async_request_refresh = AsyncMock()
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = MagicMock(coordinator=coordinator)
    async_setup_services(hass)

    await hass.services.async_call(
        DOMAIN,
        "track_parcel",
        {"tracking_code": "00340434161094681228"},
        blocking=True,
    )
    await hass.async_block_till_done()

    coordinator.async_request_refresh.assert_awaited_once()


async def test_unload_services_keeps_them_while_another_entry_is_loaded(hass):
    entry1 = _entry("e1")
    entry1.add_to_hass(hass)
    entry1.mock_state(hass, ConfigEntryState.LOADED)
    entry2 = _entry("e2")
    entry2.add_to_hass(hass)
    async_setup_services(hass)

    async_unload_services(hass)

    assert hass.services.has_service(DOMAIN, "track_parcel")
