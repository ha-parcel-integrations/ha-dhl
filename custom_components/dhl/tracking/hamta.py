"""DHL Freight Sweden's public Mitt DHL backend (hamta.dhl.com): fetch + normalize."""
from __future__ import annotations

import logging

import aiohttp

from ..const import (
    DHL_HAMTA_CODE_LENGTH,
    DHL_HAMTA_REQUEST_TIMEOUT_SECONDS,
    DHL_HAMTA_URL,
    HISTORY_MAX_EVENTS,
    NEW_ISSUE_URL,
    DHLApiError,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=DHL_HAMTA_REQUEST_TIMEOUT_SECONDS)

_HOME_DELIVERY_METHODS = frozenset(
    {"HOME_DELIVERY", "RETURN_HOME_DELIVERY", "RETURN_SERVICEPOINT"}
)

_warned_events: set[tuple[int, int]] = set()


def hamta_shaped(code: str) -> bool:
    """Whether the backend accepts ``code`` at all; it rejects any other length."""
    return len(code) == DHL_HAMTA_CODE_LENGTH


async def async_fetch_hamta(
    session: aiohttp.ClientSession, codes: list[str]
) -> dict[str, dict]:
    """Batch-fetch codes; return ``{trackingNumber: raw}``.

    A 404 means none of the batch is a Freight Sweden shipment — not an
    error. Codes it does not know are simply absent from the mapping.
    """
    codes = [c for c in codes if hamta_shaped(c)]
    if not codes:
        return {}
    try:
        async with session.get(
            DHL_HAMTA_URL,
            params=[("ids", code) for code in codes],
            timeout=_TIMEOUT,
        ) as resp:
            if resp.status == 404:
                return {}
            if resp.status != 200:
                raise DHLApiError(f"HTTP {resp.status}")
            data = await resp.json(content_type=None)
    except aiohttp.ClientError as err:
        raise DHLApiError(str(err)) from err
    if not isinstance(data, list):
        return {}
    return {
        item["trackingNumber"]: item
        for item in data
        if isinstance(item, dict) and item.get("trackingNumber")
    }


def _codes(event: dict) -> tuple[int | None, int | None]:
    return event.get("statusCode"), event.get("reasonCode")


def _is_handover(event: dict, delivery_method: str | None) -> bool:
    # Mitt DHL's own rule for which event is the hand-over to the recipient.
    status_code, reason_code = _codes(event)
    if status_code != 21:
        return False
    if delivery_method in _HOME_DELIVERY_METHODS:
        return reason_code == 0
    return reason_code in (908, 13)


def _map_event(event: dict, delivery_method: str | None) -> ParcelStatus:
    status_code, reason_code = _codes(event)
    if _is_handover(event, delivery_method):
        return ParcelStatus.DELIVERED
    if status_code == 21:
        # Dropped at the service point or locker, not yet with the recipient.
        return ParcelStatus.AT_PICKUP_POINT
    if status_code == 20 and reason_code == 907:
        return ParcelStatus.AT_PICKUP_POINT
    if status_code == 24 and reason_code == 501:
        return ParcelStatus.OUT_FOR_DELIVERY
    if status_code == 56 and reason_code == 909:
        return ParcelStatus.RETURNING
    if status_code in (1, 20, 24):
        return ParcelStatus.IN_TRANSIT
    key = (status_code, reason_code)
    if key not in _warned_events:
        _warned_events.add(key)
        _LOGGER.warning(
            "DHL Freight reported an unrecognised event %r (status %s, "
            "reason %s) — its history entry is mapped to 'unknown'. Please "
            "report this: %s",
            event.get("eventText"),
            status_code,
            reason_code,
            NEW_ISSUE_URL,
        )
    return ParcelStatus.UNKNOWN


def _map_status(raw: dict, events: list[dict]) -> ParcelStatus:
    # Same precedence as Mitt DHL's own status card: the shipment flags
    # outrank the events.
    if raw.get("isCollected"):
        return ParcelStatus.DELIVERED
    if raw.get("isTimeout"):
        return ParcelStatus.RETURNING
    if raw.get("isTerminated"):
        return ParcelStatus.PROBLEM
    if raw.get("isReadyForCollection"):
        return ParcelStatus.AT_PICKUP_POINT
    if not events:
        return ParcelStatus.REGISTERED
    status = _map_event(events[-1], raw.get("deliveryMethod"))
    if status in (ParcelStatus.UNKNOWN, ParcelStatus.DELIVERED):
        return ParcelStatus.IN_TRANSIT
    return status


def _party(raw: dict, party_type: str) -> str | None:
    for party in raw.get("parties") or []:
        if isinstance(party, dict) and party.get("type") == party_type:
            return party.get("name") or None
    return None


def normalize_parcel_hamta(raw: dict, *, include_history: bool = False) -> dict:
    """Map one Mitt DHL shipment onto the canonical parcel shape."""
    method = raw.get("deliveryMethod")
    events = sorted(
        (e for e in raw.get("events") or [] if isinstance(e, dict)),
        key=lambda e: e.get("occuredAtTime") or "",
    )
    status = _map_status(raw, events)
    delivered = status is ParcelStatus.DELIVERED

    delivered_at = None
    if delivered:
        handover = next(
            (e for e in reversed(events) if _is_handover(e, method)),
            events[-1] if events else {},
        )
        delivered_at = handover.get("occuredAtTime")

    history: list[dict] | None = None
    if include_history:
        history = [
            {
                "timestamp": event.get("occuredAtTime"),
                "status": _map_event(event, method),
                "raw_status": event.get("eventText"),
            }
            for event in events[-HISTORY_MAX_EVENTS:]
        ]

    pickup = method not in _HOME_DELIVERY_METHODS and method is not None
    service_point = raw.get("servicePoint")
    service_point = service_point if isinstance(service_point, dict) else {}

    return {
        "carrier": "DHL",
        "barcode": raw.get("trackingNumber"),
        "sender": _party(raw, "CZ"),
        "receiver": _party(raw, "CN"),
        "status": status,
        "raw_status": events[-1].get("eventText") if events else None,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": None,
        "planned_to": None,
        "pickup": pickup,
        "pickup_point": (service_point.get("name") or None) if pickup else None,
        "url": None,
        "weight": None,
        "dimensions": None,
        "history": history,
        "raw": raw,
    }
