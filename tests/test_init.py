"""Tests for DHL setup and unload."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dhl.const import (
    CONF_ACCOUNT_SUBJECT,
    CONF_COUNTRY,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    DHLApiError,
    DHLAuthError,
)

from .payloads import ACTIVE_CODE, active_sample

CLIENT = "custom_components.dhl.api.DHLApiClient"
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
