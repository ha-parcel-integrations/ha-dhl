"""Tests for the PL-specific slice of the coordinator: cookie-jar persistence."""
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dhl.const import (
    CONF_COUNTRY,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DHL_PL_COOKIES,
    CONF_TRACKED_CODES,
    DOMAIN,
)
from custom_components.dhl.coordinator import DHLCoordinator


def _pl_entry(*, cookies=None, **options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="DHL (PL)",
        unique_id="PL:501234567",
        data={CONF_COUNTRY: "PL", CONF_DHL_PL_COOKIES: cookies or []},
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
            CONF_TRACKED_CODES: [],
            **options,
        },
    )


def _pl_client(elements=None, *, exported_cookies=None) -> MagicMock:
    """A PL client mock: async_get_incoming is async, pl_session is not."""
    client = MagicMock()
    client.async_get_incoming = AsyncMock(return_value=(elements or [], False))
    client.async_get_by_number = AsyncMock(return_value=None)
    pl_session = MagicMock()
    pl_session.export_cookies = MagicMock(return_value=exported_cookies or [])
    client.pl_session = pl_session
    return client


async def test_pl_rotated_cookies_are_persisted(hass):
    entry = _pl_entry(cookies=[{"name": "access-token", "value": "old"}])
    entry.add_to_hass(hass)
    client = _pl_client(exported_cookies=[{"name": "access-token", "value": "new"}])
    coordinator = DHLCoordinator(hass, client, entry, de_session=None)

    await coordinator._async_update_data()

    assert entry.data[CONF_DHL_PL_COOKIES] == [{"name": "access-token", "value": "new"}]


async def test_pl_unchanged_cookies_are_not_rewritten(hass):
    same_cookies = [{"name": "access-token", "value": "same"}]
    entry = _pl_entry(cookies=same_cookies)
    entry.add_to_hass(hass)
    client = _pl_client(exported_cookies=same_cookies)
    coordinator = DHLCoordinator(hass, client, entry, de_session=None)

    with patch.object(
        hass.config_entries, "async_update_entry", wraps=hass.config_entries.async_update_entry
    ) as update_entry:
        await coordinator._async_update_data()

    update_entry.assert_not_called()
