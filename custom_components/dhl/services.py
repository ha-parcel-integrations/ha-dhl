"""Services for the DHL parcel tracker integration.

`dhl.track_parcel` / `dhl.untrack_parcel` add or remove a tracked number on
any DHL entry. On an account entry it is the by-number half of the inbox, for
a parcel the logged-in account does not (yet) surface — it never replaces the
account's auto-import. On a tracking or api entry it edits the same
``{tracking_code, direction}`` list the options flow manages, so an
automation can feed numbers in without opening Configure.
`dhl.update_contents` sets or clears the text describing what is in a parcel,
tracked or not, on every entry that lists the code.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
import re

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import (
    CONF_CONFIG_ENTRY_ID,
    CONF_CONTENTS,
    CONF_DIRECTION,
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKED_CODES,
    CONF_TRACKING_CODE,
    DEFAULT_DIRECTION,
    DIRECTION_INCOMING,
    DIRECTION_OUTGOING,
    DOMAIN,
    SOURCE_ACCOUNT,
    TRACKING_CODE_REGEX,
)
from .contents import ContentsStore, normalize_code
from .tracking import (
    normalize_tracking_code as normalize_code_based,
    tracked_direction,
    valid_tracking_code as valid_code_based,
)

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"
SERVICE_UPDATE_CONTENTS = "update_contents"

_CODE_RE = re.compile(TRACKING_CODE_REGEX)

_TRACK_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_DIRECTION, default=DEFAULT_DIRECTION): vol.In(
            [DIRECTION_INCOMING, DIRECTION_OUTGOING]
        ),
        vol.Optional(CONF_CONTENTS): cv.string,
        vol.Optional("config_entry_id"): cv.string,
    }
)
_UNTRACK_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional("config_entry_id"): cv.string,
    }
)
_UPDATE_CONTENTS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_CONTENTS): cv.string,
        vol.Optional(CONF_CONFIG_ENTRY_ID): cv.string,
    }
)

type _StoreContents = Callable[[str], bool]


def _no_contents(code: str) -> bool:
    return False


def _set_contents(store: ContentsStore, text: str, code: str) -> bool:
    return store.async_set(code, text)


def normalize_tracking_code(value: str) -> str:
    """Strip surrounding whitespace — users paste messily."""
    return value.strip()


def valid_tracking_code(value: str) -> bool:
    """DHL DE numbers are 8-25 alphanumeric chars — not digits-only."""
    return bool(_CODE_RE.match(value))


def _is_account(entry: ConfigEntry) -> bool:
    return entry.data.get(CONF_SOURCE, SOURCE_ACCOUNT) == SOURCE_ACCOUNT


def _resolve_entry(hass: HomeAssistant, call: ServiceCall) -> ConfigEntry:
    """Return the DHL entry the call targets, or raise a clear error.

    A single DHL entry resolves without any target field. With several, a
    lone account entry stays the default — that is what the service targeted
    before code-based entries could be picked, and existing automations rely
    on it. Anything else needs ``config_entry_id``.
    """
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError("DHL is not set up")
    entry_id = call.data.get("config_entry_id")
    if entry_id:
        for entry in entries:
            if entry.entry_id == entry_id:
                return entry
        raise ServiceValidationError(f"No DHL entry with config_entry_id {entry_id!r}")
    if len(entries) == 1:
        return entries[0]
    accounts = [entry for entry in entries if _is_account(entry)]
    if len(accounts) == 1:
        return accounts[0]
    raise ServiceValidationError(
        "More than one DHL entry is configured — pass config_entry_id"
    )


def _request_refresh(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Nudge the coordinator so the new/removed code shows up without waiting for the next poll."""
    if entry.state is ConfigEntryState.LOADED:
        hass.async_create_task(entry.runtime_data.coordinator.async_request_refresh())


def _loaded_coordinator(entry: ConfigEntry):
    """Return the coordinator of a set-up entry, or raise when it is not loaded."""
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError("DHL is not loaded")
    return entry.runtime_data.coordinator


def _contents_store(entry: ConfigEntry) -> ContentsStore | None:
    """Return the entry's contents store, or ``None`` when it is not loaded."""
    if entry.state is not ConfigEntryState.LOADED:
        return None
    return entry.runtime_data.coordinator.contents_store


def _track_on_account(
    hass: HomeAssistant,
    entry: ConfigEntry,
    raw_code: str,
    store_contents: _StoreContents = _no_contents,
) -> None:
    tracking_code = normalize_tracking_code(raw_code)
    if not valid_tracking_code(tracking_code):
        raise ServiceValidationError(
            f"'{tracking_code}' is not a valid DHL tracking code"
        )
    # Stored before the options write so the refresh it triggers sees it.
    stored = store_contents(tracking_code)
    current = list(entry.options.get(CONF_TRACKED_CODES, []))
    if tracking_code in current:
        if stored:
            _request_refresh(hass, entry)
        return
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, CONF_TRACKED_CODES: [*current, tracking_code]},
    )
    # No update listener on an account entry, so nothing else refreshes it.
    _request_refresh(hass, entry)


def _track_on_code_based(
    hass: HomeAssistant,
    entry: ConfigEntry,
    raw_code: str,
    direction: str,
    store_contents: _StoreContents = _no_contents,
) -> None:
    tracking_code = normalize_code_based(raw_code)
    if not valid_code_based(tracking_code):
        raise ServiceValidationError(
            f"'{tracking_code}' is not a valid DHL tracking code"
        )
    stored = store_contents(tracking_code)
    parcels = [dict(p) for p in entry.options.get(CONF_PARCELS, [])]
    existing = next(
        (p for p in parcels if p.get(CONF_TRACKING_CODE) == tracking_code), None
    )
    if existing is not None:
        # Calling again with the other direction moves the parcel, the same
        # correction re-entering it in the options flow makes.
        if tracked_direction(existing) == direction:
            if stored:
                _request_refresh(hass, entry)
            return
        existing[CONF_DIRECTION] = direction
    else:
        parcels.append({CONF_TRACKING_CODE: tracking_code, CONF_DIRECTION: direction})
    # The entry's update listener refreshes the coordinator.
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_PARCELS: parcels}
    )


def _untrack_on_account(hass: HomeAssistant, entry: ConfigEntry, raw_code: str) -> None:
    tracking_code = normalize_tracking_code(raw_code)
    if (store := _contents_store(entry)) is not None:
        store.async_set(tracking_code, None)
    current = list(entry.options.get(CONF_TRACKED_CODES, []))
    if tracking_code not in current:
        return
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_TRACKED_CODES: [c for c in current if c != tracking_code],
        },
    )
    _request_refresh(hass, entry)


def _untrack_on_code_based(
    hass: HomeAssistant, entry: ConfigEntry, raw_code: str
) -> None:
    tracking_code = normalize_code_based(raw_code)
    if (store := _contents_store(entry)) is not None:
        store.async_set(tracking_code, None)
    current = entry.options.get(CONF_PARCELS, [])
    kept = [p for p in current if p.get(CONF_TRACKING_CODE) != tracking_code]
    if len(kept) != len(current):
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PARCELS: kept}
        )


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the DHL services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL):
        return

    async def _track(call: ServiceCall) -> None:
        entry = _resolve_entry(hass, call)
        store_contents: _StoreContents = _no_contents
        if CONF_CONTENTS in call.data:
            store = _loaded_coordinator(entry).contents_store
            store_contents = partial(_set_contents, store, call.data[CONF_CONTENTS])

        if _is_account(entry):
            _track_on_account(
                hass, entry, call.data[CONF_TRACKING_CODE], store_contents
            )
        else:
            _track_on_code_based(
                hass,
                entry,
                call.data[CONF_TRACKING_CODE],
                call.data[CONF_DIRECTION],
                store_contents,
            )

    async def _untrack(call: ServiceCall) -> None:
        entry = _resolve_entry(hass, call)
        if _is_account(entry):
            _untrack_on_account(hass, entry, call.data[CONF_TRACKING_CODE])
        else:
            _untrack_on_code_based(hass, entry, call.data[CONF_TRACKING_CODE])

    hass.services.async_register(
        DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_TRACK_SCHEMA
    )

    async def _update_contents(call: ServiceCall) -> None:
        code = normalize_code(call.data[CONF_TRACKING_CODE])
        if not code:
            raise ServiceValidationError("A tracking code is required")
        loaded = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        ]
        if not loaded:
            raise ServiceValidationError("DHL is not set up")
        if entry_id := call.data.get(CONF_CONFIG_ENTRY_ID):
            targets = [entry for entry in loaded if entry.entry_id == entry_id]
            if not targets:
                raise ServiceValidationError(f"'{entry_id}' is not a loaded DHL entry")
        elif len(loaded) == 1:
            targets = loaded
        else:
            # A code can sit in two entries, so every entry that lists it gets
            # the value; guessing an entry would write to the wrong one.
            targets = [
                entry
                for entry in loaded
                if code in entry.runtime_data.coordinator.known_codes
            ]
            if not targets:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="update_contents_entry_required",
                )
        for entry in targets:
            coordinator = entry.runtime_data.coordinator
            if coordinator.contents_store.async_set(code, call.data.get(CONF_CONTENTS)):
                await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_UNTRACK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_CONTENTS,
        _update_contents,
        schema=_UPDATE_CONTENTS_SCHEMA,
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the DHL services once no config entry is loaded anymore."""
    if any(
        entry.state is ConfigEntryState.LOADED
        for entry in hass.config_entries.async_entries(DOMAIN)
    ):
        return
    for service in (
        SERVICE_TRACK_PARCEL,
        SERVICE_UNTRACK_PARCEL,
        SERVICE_UPDATE_CONTENTS,
    ):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
