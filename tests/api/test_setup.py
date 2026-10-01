"""Setup, isolation, attribution and diagnostics for an API entry."""
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dhl.api.client import DHLUnifiedKeyError
from custom_components.dhl.const import (
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DHL_UNIFIED_ATTRIBUTION,
    DOMAIN,
    SOURCE_API,
    SOURCE_TRACKING,
)
from custom_components.dhl.diagnostics import async_get_config_entry_diagnostics

from ..tracking.payloads import gateway_element
from .payloads import body, shipment

API_CODE = "JJD000390007000000001"
GATEWAY_CODE = "3SXYZ0000000001"
GET = "custom_components.dhl.api.client.DHLUnifiedClient.async_get_shipments"


def _api_entry(codes=(API_CODE,)) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="API (abcdef)",
        unique_id="api:abcdef123456",
        data={CONF_SOURCE: SOURCE_API, CONF_API_KEY: "secret-key"},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: c} for c in codes]},
    )


async def test_setup_and_unload(hass):
    entry = _api_entry()
    entry.add_to_hass(hass)

    with patch(GET, AsyncMock(return_value=body(shipment(API_CODE)))):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.services.has_service(DOMAIN, "track_parcel")
    state = hass.states.get("sensor.dhl_api_abcdef_incoming_parcels")
    assert state.state == "1"
    assert state.attributes["attribution"] == DHL_UNIFIED_ATTRIBUTION

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_rejected_key_at_setup_starts_reauth(hass):
    entry = _api_entry()
    entry.add_to_hass(hass)

    with patch(GET, AsyncMock(side_effect=DHLUnifiedKeyError("HTTP 401"))):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    assert flows[0]["step_id"] == "reauth_api"


async def test_options_update_refreshes_without_reload(hass):
    entry = _api_entry()
    entry.add_to_hass(hass)

    fetch = AsyncMock(return_value=body(shipment(API_CODE)))
    with patch(GET, fetch):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        hass.config_entries.async_update_entry(
            entry,
            options={CONF_PARCELS: [{CONF_TRACKING_CODE: "JJD000390007000000002"}]},
        )
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert fetch.await_count == 2


async def test_api_entry_beside_a_tracking_entry_shares_no_parcels(hass):
    api = _api_entry()
    api.add_to_hass(hass)
    tracking = MockConfigEntry(
        domain=DOMAIN,
        title="DHL tracking",
        unique_id=SOURCE_TRACKING,
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: GATEWAY_CODE}]},
    )
    tracking.add_to_hass(hass)

    with (
        patch(GET, AsyncMock(return_value=body(shipment(API_CODE)))) as api_get,
        patch(
            "custom_components.dhl.tracking.coordinator.async_fetch_gateway",
            AsyncMock(return_value={GATEWAY_CODE: gateway_element(barcode=GATEWAY_CODE)}),
        ),
        patch(
            "custom_components.dhl.tracking.coordinator.async_fetch_mojdhl",
            AsyncMock(return_value={}),
        ),
    ):
        # Setting up one entry sets up the whole domain, so both load here.
        assert await hass.config_entries.async_setup(api.entry_id)
        await hass.async_block_till_done()
    assert tracking.state is ConfigEntryState.LOADED

    assert [c.args[0] for c in api_get.await_args_list] == [API_CODE]
    assert [p["barcode"] for p in api.runtime_data.coordinator.data] == [API_CODE]
    assert [p["barcode"] for p in tracking.runtime_data.coordinator.data] == [GATEWAY_CODE]

    registry = er.async_get(hass)
    tracking_ids = {
        e.unique_id for e in er.async_entries_for_config_entry(registry, tracking.entry_id)
    }
    assert not any(API_CODE in uid for uid in tracking_ids)
    tracking_state = hass.states.get("sensor.dhl_dhl_tracking_incoming_parcels")
    assert tracking_state.attributes["attribution"] != DHL_UNIFIED_ATTRIBUTION


async def test_diagnostics_redact_the_key_codes_and_addresses(hass):
    entry = _api_entry()
    entry.add_to_hass(hass)
    with patch(GET, AsyncMock(return_value=body(shipment(API_CODE)))):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    dumped = repr(diag)

    assert "secret-key" not in dumped
    assert API_CODE not in dumped
    assert "10115" not in dumped
    assert "Berlin" not in dumped
    assert diag["api"] == {
        "delivered_codes": 0,
        "consecutive_rate_limits": 0,
        "max_retention_days": 30,
    }
    assert diag["tracking"] is None
    raw = diag["incoming"][0]["raw"]
    # Leaves are redacted, the structure around them survives.
    assert raw["destination"]["address"]["countryCode"] == "DE"
    assert raw["destination"]["address"]["postalCode"] == "**REDACTED**"
