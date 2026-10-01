"""Map one Unified API shipment onto the canonical parcel shape — pure, no I/O.

Built against DHL's OpenAPI spec, not a captured payload. The status is the
five-value ``statusCode`` baseline; the fine ``status`` text only refines it
once a real parcel has shown which text means what, so ``_REFINEMENTS`` stays
empty until then. Timestamps are passed through exactly as DHL sends them:
per the spec, one without an offset is local to the event's place, so it is
never relabelled as UTC.
"""
from __future__ import annotations

import logging
from datetime import datetime

from ..const import HISTORY_MAX_EVENTS, NEW_ISSUE_URL, ParcelStatus
from ..tracking.parcels import parse_iso

_LOGGER = logging.getLogger(__name__)

_STATUS_CODES: dict[str, ParcelStatus] = {
    "pre-transit": ParcelStatus.REGISTERED,
    "transit": ParcelStatus.IN_TRANSIT,
    "delivered": ParcelStatus.DELIVERED,
    "failure": ParcelStatus.PROBLEM,
    "unknown": ParcelStatus.UNKNOWN,
}

# (service, status text) -> status, for texts a real parcel has shown.
_REFINEMENTS: dict[tuple[str, str], ParcelStatus] = {}

_KG_PER_UNIT = {
    "kg": 1.0,
    "g": 0.001,
    "lb": 0.45359237,
    "lbs": 0.45359237,
    "oz": 0.028349523125,
}

_warned_status_codes: set[str] = set()
_warned_multiple_shipments = False
_warned_weight_units: set[str] = set()


def pick_shipment(body: dict, code: str) -> dict | None:
    """Return the shipment for ``code`` out of a response body.

    The one whose ``id`` equals the code, else the first. Whether one number
    can resolve to several shipments is not known yet, so that case warns once.
    """
    global _warned_multiple_shipments  # noqa: PLW0603
    shipments = [s for s in body.get("shipments") or [] if isinstance(s, dict)]
    if not shipments:
        return None
    if len(shipments) > 1 and not _warned_multiple_shipments:
        _warned_multiple_shipments = True
        _LOGGER.warning(
            "DHL's API returned %s shipments for one tracking code; showing the "
            "one matching the code, else the first. Please report this: %s",
            len(shipments),
            NEW_ISSUE_URL,
        )
    for shipment in shipments:
        if str(shipment.get("id", "")).upper() == code.upper():
            return shipment
    return shipments[0]


def _warn_status_code(value: str) -> None:
    if value in _warned_status_codes:
        return
    _warned_status_codes.add(value)
    _LOGGER.warning(
        "DHL's API reported an unrecognised statusCode %r — mapped to "
        "'unknown'. Please report this: %s",
        value,
        NEW_ISSUE_URL,
    )


def _map_status(service: str | None, status: dict) -> ParcelStatus:
    refined = _REFINEMENTS.get((service or "", status.get("status") or ""))
    if refined is not None:
        return refined
    code = status.get("statusCode")
    mapped = _STATUS_CODES.get(code) if isinstance(code, str) else None
    if mapped is None:
        if code:
            _warn_status_code(str(code))
        return ParcelStatus.UNKNOWN
    return mapped


def _weight_kg(details: dict) -> float | None:
    weight = details.get("weight")
    if not isinstance(weight, dict):
        return None
    value = weight.get("value")
    unit = str(weight.get("unitText") or "").strip().lower()
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    factor = _KG_PER_UNIT.get(unit)
    if factor is None:
        if unit not in _warned_weight_units:
            _warned_weight_units.add(unit)
            _LOGGER.warning(
                "DHL's API reported a weight in an unrecognised unit %r — "
                "weight left empty. Please report this: %s",
                unit,
                NEW_ISSUE_URL,
            )
        return None
    return round(value * factor, 3)


def _events_newest_first(events: list) -> list[dict]:
    # Only the order matters here, so an offset-less timestamp is compared as
    # if it were UTC; an unparseable one sorts last.
    dated: list[tuple[datetime, dict]] = []
    undated: list[dict] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        parsed = parse_iso(event.get("timestamp"))
        if parsed is None:
            undated.append(event)
        else:
            dated.append((parsed, event))
    dated.sort(key=lambda item: item[0], reverse=True)
    return [event for _, event in dated] + undated


def normalize_parcel_unified(
    shipment: dict, *, code: str, include_history: bool = False
) -> dict:
    """Map one Unified API shipment (or a not-yet-found placeholder) to a parcel."""
    service = shipment.get("service")
    status_obj = shipment.get("status") if isinstance(shipment.get("status"), dict) else {}
    details = shipment.get("details") if isinstance(shipment.get("details"), dict) else {}
    events = _events_newest_first(shipment.get("events") or [])

    status = _map_status(service, status_obj) if status_obj else ParcelStatus.UNKNOWN
    if status_obj.get("description"):
        _LOGGER.debug(
            "DHL Unified API status %r: %s",
            status_obj.get("status"),
            status_obj.get("description"),
        )

    delivered = status_obj.get("statusCode") == "delivered"
    delivered_at = None
    if delivered:
        delivered_at = next(
            (e.get("timestamp") for e in events if e.get("statusCode") == "delivered"),
            status_obj.get("timestamp"),
        )

    planned_from = planned_to = None
    if not delivered:
        frame = shipment.get("estimatedDeliveryTimeFrame")
        if isinstance(frame, dict) and (
            frame.get("estimatedFrom") or frame.get("estimatedThrough")
        ):
            planned_from = frame.get("estimatedFrom")
            planned_to = frame.get("estimatedThrough")
        elif shipment.get("estimatedTimeOfDelivery"):
            planned_from = planned_to = shipment["estimatedTimeOfDelivery"]

    history: list[dict] | None = None
    if include_history:
        history = [
            {
                "timestamp": event.get("timestamp"),
                "status": _map_status(service, event),
                "raw_status": event.get("status"),
            }
            for event in events[:HISTORY_MAX_EVENTS]
        ]

    return {
        "carrier": "DHL",
        "barcode": shipment.get("id") or code,
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": status_obj.get("status"),
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": False,
        "pickup_point": None,
        "url": None,
        "weight": _weight_kg(details),
        "dimensions": None,
        "history": history,
        "raw": shipment,
    }
