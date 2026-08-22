"""Services for the DHL parcel tracker integration.

`dhl.track_parcel` / `dhl.untrack_parcel` add or remove a manually-tracked
tracking number — the by-number half of the account inbox
(BUILD_PLAN.md §4), for a parcel that is not (or not yet) in the logged-in
account's own inbox. Unlike an account-less carrier's `track_parcel`, this
never replaces the account's auto-import; it only adds numbers the account
would not otherwise surface.
"""
from __future__ import annotations

import re

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import CONF_TRACKED_CODES, CONF_TRACKING_CODE, DOMAIN, TRACKING_CODE_REGEX

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"

_CODE_RE = re.compile(TRACKING_CODE_REGEX)

_TRACK_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional("config_entry_id"): cv.string,
    }
)
_UNTRACK_SCHEMA = _TRACK_SCHEMA


def normalize_tracking_code(value: str) -> str:
    """Strip surrounding whitespace — users paste messily."""
    return value.strip()


def valid_tracking_code(value: str) -> bool:
    """DHL DE numbers are 8-25 alphanumeric chars (BUILD_PLAN.md §4) — not digits-only."""
    return bool(_CODE_RE.match(value))


def _resolve_entry(hass: HomeAssistant, call: ServiceCall):
    """Return the DHL account the call targets, or raise a clear error.

    A single configured account resolves without any target field; more
    than one requires ``config_entry_id`` to disambiguate.
    """
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError("DHL is not set up")
    entry_id = call.data.get("config_entry_id")
    if entry_id:
        for entry in entries:
            if entry.entry_id == entry_id:
                return entry
        raise ServiceValidationError(f"No DHL account with config_entry_id {entry_id!r}")
    if len(entries) > 1:
        raise ServiceValidationError(
            "More than one DHL account is configured — pass config_entry_id"
        )
    return entries[0]


def _request_refresh(hass: HomeAssistant, entry) -> None:
    """Nudge the coordinator so the new/removed code shows up without waiting for the next poll."""
    if entry.state is ConfigEntryState.LOADED:
        hass.async_create_task(
            entry.runtime_data.coordinator.async_request_refresh()
        )


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the DHL services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL):
        return

    async def _track(call: ServiceCall) -> None:
        tracking_code = normalize_tracking_code(call.data[CONF_TRACKING_CODE])
        if not valid_tracking_code(tracking_code):
            raise ServiceValidationError(
                f"'{tracking_code}' is not a valid DHL tracking code"
            )
        entry = _resolve_entry(hass, call)
        current = list(entry.options.get(CONF_TRACKED_CODES, []))
        if tracking_code in current:
            return  # already tracked — no-op
        hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, CONF_TRACKED_CODES: [*current, tracking_code]},
        )
        _request_refresh(hass, entry)

    async def _untrack(call: ServiceCall) -> None:
        tracking_code = normalize_tracking_code(call.data[CONF_TRACKING_CODE])
        entry = _resolve_entry(hass, call)
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

    hass.services.async_register(
        DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_TRACK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_UNTRACK_SCHEMA
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the DHL services once no config entry is loaded anymore."""
    if any(
        entry.state is ConfigEntryState.LOADED
        for entry in hass.config_entries.async_entries(DOMAIN)
    ):
        return
    for service in (SERVICE_TRACK_PARCEL, SERVICE_UNTRACK_PARCEL):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
