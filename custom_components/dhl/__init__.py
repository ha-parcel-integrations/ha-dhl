"""DHL parcel tracker custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DHLApiClient
from .const import CONF_COUNTRY, CONF_REFRESH_TOKEN, DEFAULT_COUNTRY, PLATFORMS
from .coordinator import DHLCoordinator
from .countries.de.session import DHLDeSession
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)


@dataclass
class DHLData:
    """Runtime data attached to a DHL config entry."""

    client: DHLApiClient
    coordinator: DHLCoordinator
    de_session: DHLDeSession
    session: aiohttp.ClientSession


type DHLConfigEntry = ConfigEntry[DHLData]


async def async_setup_entry(hass: HomeAssistant, entry: DHLConfigEntry) -> bool:
    """Set up DHL from a config entry.

    Only DE is dispatched today — see countries/__init__.py for the shape a
    second country would add to.
    """
    country = entry.data.get(CONF_COUNTRY, DEFAULT_COUNTRY)

    # Each config entry needs its own cookie jar, or two accounts overwrite
    # each other's dhli cookie in the shared session. The connector is reused
    # (connector_owner=False) so this stays cheap.
    session = aiohttp.ClientSession(
        connector=async_get_clientsession(hass).connector,
        connector_owner=False,
        cookie_jar=aiohttp.CookieJar(),
    )
    de_session = DHLDeSession(session, refresh_token=entry.data.get(CONF_REFRESH_TOKEN))
    client = DHLApiClient(session, country=country, de_session=de_session)
    coordinator = DHLCoordinator(hass, client, entry, de_session=de_session)

    try:
        # Fetch initial data here, before forwarding to platforms. Raising
        # ConfigEntryNotReady/ConfigEntryAuthFailed from a forwarded platform
        # is too late for HA to catch cleanly (it logs a warning and
        # half-sets-up the entry); doing the first refresh here lets a
        # transient failure — or a rejected refresh token — fail the whole
        # entry so HA retries it (or starts reauth) cleanly.
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        # Without this, every setup retry leaks a session.
        await session.close()
        raise

    entry.runtime_data = DHLData(
        client=client, coordinator=coordinator, de_session=de_session, session=session
    )

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await session.close()
        raise

    async_setup_services(hass)

    # No entry.add_update_listener: the options flow calls
    # async_schedule_reload itself. Combining an update listener with a
    # reload-on-update flow is deprecated and becomes an error in HA 2026.12+.
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DHLConfigEntry) -> bool:
    """Unload a DHL config entry."""
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.session.close()
        async_unload_services(hass)
        return True
    return False
