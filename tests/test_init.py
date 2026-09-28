"""Tests for DHL setup and unload."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dhl.const import (
    CONF_ACCOUNT_SUBJECT,
    CONF_COUNTRY,
    CONF_PARCELS,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_TRACKING,
    TRACKING_STORAGE_KEY,
    DHLApiError,
    DHLAuthError,
)

from .payloads import ACTIVE_CODE, active_sample
from .tracking.payloads import gateway_element

CLIENT = "custom_components.dhl.account.client.DHLApiClient"
SESSION_CLASS = "custom_components.dhl.config_flow.DHLDeSession"


def _dummy_oidc_session() -> MagicMock:
    """A never-network-touching stand-in for the reauth flow's OIDC session.

    The setup tests below only need reauth to *start*, not to complete — the
    real paste flow is covered end to end in test_config_flow.py.
    """
    session = MagicMock()
    session.async_authorization_url = AsyncMock(
        return_value=("https://login.dhl.de/authorize", "verifier", "state")
    )
    return session


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Germany",
        unique_id="DE:subject-abc",
        data={
            CONF_COUNTRY: "DE",
            CONF_REFRESH_TOKEN: "refresh-token",
            CONF_ACCOUNT_SUBJECT: "subject-abc",
        },
    )


async def test_setup_and_unload(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    with patch(
        f"{CLIENT}.async_get_incoming",
        new=AsyncMock(return_value=([active_sample()], False)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    incoming = hass.states.get("sensor.dhl_germany_incoming_parcels")
    assert incoming is not None
    assert incoming.state == "1"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_rejected_refresh_token_starts_reauth(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        patch(
            f"{CLIENT}.async_get_incoming",
            new=AsyncMock(side_effect=DHLAuthError("refresh token rejected")),
        ),
        patch(SESSION_CLASS, return_value=_dummy_oidc_session()),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )


@pytest.mark.parametrize("error", [DHLApiError("HTTP 500"), TimeoutError("boom")])
async def test_outage_retries_instead_of_reauth(hass, error):
    """A transient outage must retry with backoff — never push the user into reauth."""
    entry = _entry()
    entry.add_to_hass(hass)

    with patch(f"{CLIENT}.async_get_incoming", new=AsyncMock(side_effect=error)):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress()


async def test_failed_platform_setup_closes_the_session(hass):
    """Every failed-setup path must close the per-entry session, or each retry leaks one."""
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        patch(
            f"{CLIENT}.async_get_incoming",
            new=AsyncMock(return_value=([active_sample()], False)),
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(side_effect=RuntimeError("platform blew up")),
        ),
        patch("aiohttp.ClientSession.close", new=AsyncMock()) as close,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    close.assert_awaited()


async def test_per_parcel_sensor_spawn_and_remove(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    incoming = AsyncMock(return_value=([active_sample()], False))
    with patch(f"{CLIENT}.async_get_incoming", new=incoming):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{ACTIVE_CODE}"
        )

        incoming.return_value = ([active_sample("SECOND000001")], False)
        await entry.runtime_data.coordinator.async_request_refresh()
        await hass.async_block_till_done()

        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_SECOND000001"
        )
        assert (
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_{ACTIVE_CODE}"
            )
            is None
        )


def _tracking_entry(codes: list[str] | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="DHL tracking",
        unique_id=SOURCE_TRACKING,
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: c} for c in (codes or [])]},
    )


async def test_tracking_setup_and_unload(hass):
    entry = _tracking_entry(["3SXYZ0000000001"])
    entry.add_to_hass(hass)

    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_gateway",
        AsyncMock(return_value={"3SXYZ0000000001": gateway_element(barcode="3SXYZ0000000001")}),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    # No dedicated services for the tracking source — parcels are managed
    # through this entry's own options flow instead.
    assert not hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_removing_a_tracking_entry_deletes_its_cache(hass, hass_storage):
    entry = _tracking_entry(["3SXYZ0000000001"])
    entry.add_to_hass(hass)
    key = f"{TRACKING_STORAGE_KEY}.{entry.entry_id}"
    hass_storage[key] = {"version": 1, "key": key, "data": {}}

    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_gateway",
        AsyncMock(return_value={"3SXYZ0000000001": gateway_element(barcode="3SXYZ0000000001")}),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


async def test_tracking_options_update_refreshes_without_reload(hass):
    entry = _tracking_entry(["3SXYZ0000000001"])
    entry.add_to_hass(hass)

    fetch = AsyncMock(return_value={"3SXYZ0000000001": gateway_element(barcode="3SXYZ0000000001")})
    with patch("custom_components.dhl.tracking.coordinator.async_fetch_gateway", fetch):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        hass.config_entries.async_update_entry(
            entry, options={CONF_PARCELS: [{CONF_TRACKING_CODE: "3SXYZ0000000002"}]}
        )
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert fetch.await_count >= 2


async def test_services_registered_and_removed_with_last_entry(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    with patch(
        f"{CLIENT}.async_get_incoming",
        new=AsyncMock(return_value=([], False)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert hass.services.has_service(DOMAIN, "track_parcel")

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        assert not hass.services.has_service(DOMAIN, "track_parcel")
