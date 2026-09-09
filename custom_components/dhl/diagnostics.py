"""Diagnostics support for the DHL parcel tracker integration.

This export is not only a support tool here — it is the instrument the
payload mapping gets *finished* with: nobody in this suite has ever seen a
populated ``sendungen`` element, so a tester's diagnostics download is what
corrects that mapping (see countries/de/__init__.py's module docstring).
**Redact values, never structure** — a redacted string stays a string, a
redacted number stays a number, and no key is ever dropped. See
``tests/countries/test_de.py``'s redaction test, which asserts the key set
survives untouched.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import DHLConfigEntry
from .const import CONF_TRACKED_CODES

# Redact values, keep every key — a missing key would be indistinguishable
# from a key the API never sent, which is exactly the ambiguity this export
# exists to remove.
#
# Deliberately does NOT include "zustellung" or "empfaenger": those are
# objects, and async_redact_data replaces a redacted key's whole value —
# blanket-redacting the container would collapse
# sendungsdetails.zustellung.empfaenger.name's nesting into a string, which
# is precisely the trap a naive top-level redactor falls into. Redacting the
# leaf "name" key reaches
# it without losing the structure around it.
TO_REDACT = {
    # canonical fields we publish ourselves
    "tracking_code",
    "barcode",
    "sender",
    "receiver",
    "url",
    # the DE payload's own field names — "id" is the tracking number,
    # "name" is sendungsdetails.zustellung.empfaenger.name
    "id",
    "name",
    "sendungsnummer",
    # the account holder's own email address, present on every element
    "email",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DHLConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the DHL config entry."""
    coordinator = entry.runtime_data.coordinator

    # CONF_TRACKED_CODES is a bare list of strings, not a list of dicts — a
    # key-name redactor like async_redact_data has no key to match inside
    # each element, so it would leak tracking numbers straight through.
    # Redact it manually, preserving the array length.
    entry_options = dict(entry.options)
    tracked_codes = entry_options.get(CONF_TRACKED_CODES)
    if isinstance(tracked_codes, list):
        entry_options[CONF_TRACKED_CODES] = ["**REDACTED**" for _ in tracked_codes]

    interval = coordinator.update_interval
    return {
        "entry_options": async_redact_data(entry_options, TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
            "outgoing_active": len(coordinator.outgoing or []),
            "outgoing_delivered": len(coordinator.delivered_outgoing or []),
        },
        "polling": {
            "interval_minutes": (
                round(interval.total_seconds() / 60, 1) if interval else None
            ),
        },
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "delivered": async_redact_data(coordinator.delivered or [], TO_REDACT),
        "outgoing": async_redact_data(coordinator.outgoing or [], TO_REDACT),
        "outgoing_delivered": async_redact_data(
            coordinator.delivered_outgoing or [], TO_REDACT
        ),
    }
