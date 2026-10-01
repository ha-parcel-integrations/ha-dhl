"""DHL parcel tracker custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .account.client import DHLApiClient
from .account.coordinator import DHLCoordinator
from .account.countries.de.session import DHLDeSession
from .account.countries.pl.session import DHLPlSession
from .api.client import DHLUnifiedClient
from .api.coordinator import DHLUnifiedCoordinator
from .const import (
    CONF_COUNTRY,
    CONF_DHL_PL_COOKIES,
    CONF_DHL_PL_DEVICE_ID,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    DEFAULT_COUNTRY,
    PLATFORMS,
    SOURCE_ACCOUNT,
    SOURCE_API,
    SOURCE_TRACKING,
    TRACKING_STORAGE_KEY,
    TRACKING_STORAGE_VERSION,
)
from .services import async_setup_services, async_unload_services
from .tracking.coordinator import DHLTrackingCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass
class DHLData:
    """Runtime data attached to a DHL config entry."""

    client: object
    coordinator: DHLCoordinator | DHLTrackingCoordinator | DHLUnifiedCoordinator
    de_session: DHLDeSession | None
    pl_session: DHLPlSession | None
    session: aiohttp.ClientSession


type DHLConfigEntry = ConfigEntry[DHLData]


async def async_setup_entry(hass: HomeAssistant, entry: DHLConfigEntry) -> bool:
    """Set up DHL from a config entry.

    An entry with no ``CONF_SOURCE`` predates the tracking-code flow and is
    an account entry — the account branch below is the default, never a
    migration.
    """
    source = entry.data.get(CONF_SOURCE, SOURCE_ACCOUNT)
    if source == SOURCE_API:
        session = async_get_clientsession(hass)
        client = DHLUnifiedClient(session, entry.data[CONF_API_KEY])
        coordinator = DHLUnifiedCoordinator(hass, client, entry)
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = DHLData(
            client=client,
            coordinator=coordinator,
            de_session=None,
            pl_session=None,
            session=session,
        )
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        async_setup_services(hass)
        # Same live options model as the tracking source.
        entry.async_on_unload(entry.add_update_listener(_async_tracking_options_updated))
        return True

    if source == SOURCE_TRACKING:
        # The tracking source needs no dedicated cookie jar — HA's shared
        # session is fine.
        session = async_get_clientsession(hass)
        client = session
        coordinator = DHLTrackingCoordinator(
            hass, client, entry
        )
        de_session = None
        pl_session = None

        await coordinator.async_load_cache()
        await coordinator.async_config_entry_first_refresh()

        entry.runtime_data = DHLData(
            client=client,
            coordinator=coordinator,
            de_session=de_session,
            pl_session=pl_session,
            session=session,
        )
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        async_setup_services(hass)

        # Unlike the account source's options flow (which calls
        # async_schedule_reload itself), tracking-mode options are applied
        # live via a coordinator refresh — adding/removing a code, from the
        # options flow or `dhl.track_parcel`, shows up immediately, matching
        # ha-bpost's tracking source.
        entry.async_on_unload(entry.add_update_listener(_async_tracking_options_updated))

        return True

    country = entry.data.get(CONF_COUNTRY, DEFAULT_COUNTRY)

    # Each config entry needs its own cookie jar, or two accounts overwrite
    # each other's dhli cookie in the shared session. The connector is reused
    # (connector_owner=False) so this stays cheap.
    session = aiohttp.ClientSession(
        connector=async_get_clientsession(hass).connector,
        connector_owner=False,
        cookie_jar=aiohttp.CookieJar(),
    )
    de_session = DHLDeSession(session, refresh_token=entry.data.get(CONF_REFRESH_TOKEN)) if country == "DE" else None
    pl_session = DHLPlSession(session, entry.data.get(CONF_DHL_PL_COOKIES)) if country == "PL" else None
    client = DHLApiClient(session, country=country, de_session=de_session,
                          pl_session=pl_session, pl_device_id=entry.data.get(CONF_DHL_PL_DEVICE_ID))
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
        client=client, coordinator=coordinator, de_session=de_session, pl_session=pl_session, session=session
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


async def _async_tracking_options_updated(
    hass: HomeAssistant, entry: DHLConfigEntry
) -> None:
    """Apply a code-based source's options change by refreshing the coordinator."""
    await entry.runtime_data.coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: DHLConfigEntry) -> bool:
    """Unload a DHL config entry."""
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        if entry.data.get(CONF_SOURCE, SOURCE_ACCOUNT) == SOURCE_ACCOUNT:
            # The code-based sources reuse HA's shared session — nothing
            # owned to close. The account source opens its own cookie-jarred
            # one.
            await entry.runtime_data.session.close()
        async_unload_services(hass)
        return True
    return False


async def async_remove_entry(hass: HomeAssistant, entry: DHLConfigEntry) -> None:
    """Delete a tracking entry's persisted cache when the entry is removed."""
    if entry.data.get(CONF_SOURCE, SOURCE_ACCOUNT) == SOURCE_TRACKING:
        await Store(
            hass,
            TRACKING_STORAGE_VERSION,
            f"{TRACKING_STORAGE_KEY}.{entry.entry_id}",
        ).async_remove()
