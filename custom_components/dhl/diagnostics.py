"""Diagnostics support for the DHL parcel tracker integration.

For the account source this export is not only a support tool — it is the
instrument the payload mapping gets *finished* with (see
account/countries/de/__init__.py's module docstring). **Redact values, never
structure** — a redacted string stays a string, a redacted number stays a
number, and no key is ever dropped. See
``tests/account/countries/test_de.py``'s redaction test, which asserts the
key set survives untouched.

The tracking source shares the same shape (``incoming``/``delivered``/
``counts``), plus a ``tracking`` block reporting the Express half's
budget/backoff state. The API source adds an ``api`` block instead, and its
``raw`` shipments carry addresses and names, redacted leaf by leaf below.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant

from . import DHLConfigEntry
from .const import (
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKED_CODES,
    DHL_UNIFIED_MAX_RETENTION_DAYS,
    SOURCE_API,
    SOURCE_TRACKING,
)

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
    # DHL Freight Sweden's shipment: its numbers, the parties' towns and the
    # service point near the recipient
    "trackingNumber",
    "parcelId",
    "pickup_point",
    "address",
    "address2",
    "zipCode",
    "city",
    "latitude",
    "longitude",
    # DHL Parcel Polska's shipment number and the PIN that collects the parcel
    "shipmentNumber",
    "shipmentPin",
    # the account holder's own email address, present on every element
    "email",
    "phone",
    "sms_code",
    "pl_phone",
    "pl_device_id",
    "pl_cookies",
    "access-token",
    "access-signature",
    "token",
    CONF_API_KEY,
}

# An API entry's shipments nest their addresses as objects under "address",
# so that key is swapped for the leaves inside it.
TO_REDACT_API = (TO_REDACT - {"address"}) | {
    "postalCode",
    "streetAddress",
    "addressLine",
    "addressLocality",
    "addressLocalityServicing",
    "addressRegion",
    "familyName",
    "givenName",
    "organizationName",
    "number",
    "documentUrl",
    "signatureUrl",
    "serviceUrl",
    "rerouteUrl",
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
    parcels = entry_options.get(CONF_PARCELS)
    if isinstance(parcels, list):
        entry_options[CONF_PARCELS] = [
            {**parcel, "tracking_code": "**REDACTED**"}
            if isinstance(parcel, dict)
            else "**REDACTED**"
            for parcel in parcels
        ]

    interval = coordinator.update_interval
    de_session = coordinator.de_session
    is_tracking = entry.data.get(CONF_SOURCE) == SOURCE_TRACKING
    tracking_info = None
    if is_tracking:
        tracking_info = {
            "express_budget_available": coordinator.express_budget_available,
            "express_consecutive_failures": coordinator.express_consecutive_failures,
            "express_standing_down": coordinator.express_standing_down,
            "express_disabled": coordinator.express_disabled,
        }
    api_info = None
    to_redact = TO_REDACT
    if entry.data.get(CONF_SOURCE) == SOURCE_API:
        to_redact = TO_REDACT_API
        api_info = {
            "delivered_codes": len(coordinator.delivered_codes),
            "consecutive_rate_limits": coordinator.consecutive_rate_limits,
            "max_retention_days": DHL_UNIFIED_MAX_RETENTION_DAYS,
        }
    return {
        "entry_options": async_redact_data(entry_options, to_redact),
        # Claim *names* only, never their values — `post_number` and `email`
        # are PII. Their presence is what tells an emptied inbox apart from
        # an empty account.
        "session": {
            "id_token_claims": (
                de_session.id_token_claim_names if de_session else None
            ),
            "id_token_expires_at": (
                de_session.id_token_expires_at.isoformat()
                if de_session and de_session.id_token_expires_at
                else None
            ),
            "last_refresh_at": (
                de_session.last_refresh_at.isoformat()
                if de_session and de_session.last_refresh_at
                else None
            ),
            "last_inbox_elements": coordinator.last_element_count,
        },
        "tracking": tracking_info,
        "api": api_info,
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
        "incoming": async_redact_data(coordinator.data or [], to_redact),
        "delivered": async_redact_data(coordinator.delivered or [], to_redact),
        "outgoing": async_redact_data(coordinator.outgoing or [], to_redact),
        "outgoing_delivered": async_redact_data(
            coordinator.delivered_outgoing or [], to_redact
        ),
    }
