"""DHL eCommerce Iberia's public tracking (clientesparcel.dhl.es): fetch + normalize."""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging

import aiohttp

from ..const import (
    DHL_IBERIA_HEADERS,
    DHL_IBERIA_MAX_PARALLEL_REQUESTS,
    DHL_IBERIA_REQUEST_TIMEOUT_SECONDS,
    DHL_IBERIA_URL,
    HISTORY_MAX_EVENTS,
    NEW_ISSUE_URL,
    DHLApiError,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=DHL_IBERIA_REQUEST_TIMEOUT_SECONDS)

# An unknown number is an empty 204; a number the gateway cannot parse at all
# is a 409.
_NOT_FOUND = frozenset({204, 404, 409})

_CODE_MAP: dict[str, ParcelStatus] = {
    "BS": ParcelStatus.IN_TRANSIT,
    "HS": ParcelStatus.IN_TRANSIT,
    "AS": ParcelStatus.IN_TRANSIT,
    "ES": ParcelStatus.IN_TRANSIT,
    "GT": ParcelStatus.IN_TRANSIT,
    "DIA": ParcelStatus.IN_TRANSIT,
    "EC": ParcelStatus.IN_TRANSIT,
    "ZZZ": ParcelStatus.IN_TRANSIT,
    "A": ParcelStatus.OUT_FOR_DELIVERY,
    "IS": ParcelStatus.AT_PICKUP_POINT,
    "NH": ParcelStatus.PROBLEM,
    "CR": ParcelStatus.PROBLEM,
    "CH": ParcelStatus.PROBLEM,
    "PE": ParcelStatus.PROBLEM,
    "FM": ParcelStatus.PROBLEM,
    "OH": ParcelStatus.PROBLEM,
    "FA": ParcelStatus.PROBLEM,
    "AI": ParcelStatus.PROBLEM,
    "RT": ParcelStatus.RETURNING,
    "R": ParcelStatus.DELIVERED,
    "PC": ParcelStatus.DELIVERED,
    "RS": ParcelStatus.DELIVERED,
}

# Scans that carry no code are told apart by their English wording only.
_CODELESS_MAP: dict[str, ParcelStatus] = {
    "Shipment received at DHL Depot": ParcelStatus.IN_TRANSIT,
    "Arrived at": ParcelStatus.IN_TRANSIT,
    "In transit in": ParcelStatus.IN_TRANSIT,
    "Departed from": ParcelStatus.IN_TRANSIT,
    "Picked up": ParcelStatus.IN_TRANSIT,
}

# Codes that put the parcel on its way to, or at, a ServicePoint.
_PICKUP_CODES = frozenset({"AS", "ES", "IS", "RS"})

# Depot scans end in a preposition; the page completes them with the town.
_OPEN_ENDED = (" at", " in", " from")

_warned: set[str] = set()


def iberia_shaped(code: str) -> bool:
    """Whether ``code`` may be sent at all; an empty number returns another parcel."""
    return bool(code) and code.isalnum()


async def _fetch_one(session: aiohttp.ClientSession, code: str) -> dict | None:
    try:
        async with session.get(
            DHL_IBERIA_URL,
            params={"number": code},
            headers=DHL_IBERIA_HEADERS,
            timeout=_TIMEOUT,
        ) as resp:
            if resp.status in _NOT_FOUND:
                return None
            if resp.status != 200:
                raise DHLApiError(f"HTTP {resp.status}")
            if resp.content_type != "application/json":
                # The firewall answers a challenge page under 200.
                raise DHLApiError(f"unexpected {resp.content_type or 'empty'} answer")
            data = await resp.json(content_type=None)
    except aiohttp.ClientError as err:
        raise DHLApiError(str(err)) from err
    if not isinstance(data, dict):
        return None
    # The answer is only bound to the number it echoes back.
    if str(data.get("ExpeditionNumber") or "").upper() != code:
        return None
    return data


async def async_fetch_iberia(
    session: aiohttp.ClientSession, codes: list[str]
) -> dict[str, dict]:
    """Fetch codes in parallel; return ``{code: raw}`` for the ones Iberia knows.

    One failing code does not cost the others their answer; only when every
    request failed is that raised.
    """
    codes = [c for c in codes if iberia_shaped(c)]
    if not codes:
        return {}
    limit = asyncio.Semaphore(DHL_IBERIA_MAX_PARALLEL_REQUESTS)

    async def fetch(code: str) -> dict | None:
        async with limit:
            return await _fetch_one(session, code)

    answers = await asyncio.gather(*(fetch(c) for c in codes), return_exceptions=True)
    errors = [a for a in answers if isinstance(a, BaseException)]
    for error in errors:
        if not isinstance(error, DHLApiError):
            raise error
    if errors and len(errors) == len(codes):
        raise errors[0]
    for error in errors:
        _LOGGER.debug("DHL Iberia request failed for one code: %s", error)
    return {
        code: answer
        for code, answer in zip(codes, answers, strict=True)
        if isinstance(answer, dict)
    }


def _timestamp(event: dict) -> str | None:
    # Local wall time without an offset: the network spans mainland Spain,
    # the Canary Islands and Portugal, and the scan does not say which.
    try:
        parsed = datetime.strptime(
            f"{event.get('Date')} {event.get('Time')}", "%d/%m/%Y %H:%M"
        )
    except ValueError:
        return None
    return parsed.isoformat()


def _wording(event: dict) -> str | None:
    description = event.get("Description")
    if not isinstance(description, str) or not description:
        return None
    town = event.get("Town")
    if description.endswith(_OPEN_ENDED) and isinstance(town, str) and town:
        return f"{description} {town}"
    return description


def _map_event(event: dict) -> ParcelStatus:
    code = event.get("Code")
    if code:
        if code in _CODE_MAP:
            return _CODE_MAP[code]
        key = f"code {code}"
    elif event.get("SolutionCode") == "DOC":
        return ParcelStatus.REGISTERED
    else:
        description = event.get("Description") or ""
        if description in _CODELESS_MAP:
            return _CODELESS_MAP[description]
        key = f"text {description!r}"
    if key not in _warned:
        _warned.add(key)
        _LOGGER.warning(
            "DHL Iberia reported an unrecognised scan (%s) — it is mapped to "
            "'unknown'. Please report this: %s",
            key,
            NEW_ISSUE_URL,
        )
    return ParcelStatus.UNKNOWN


def _weight(value) -> float | None:
    try:
        weight = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return weight if weight > 0 else None


def normalize_parcel_iberia(raw: dict, *, include_history: bool = False) -> dict:
    """Map one DHL eCommerce Iberia shipment onto the canonical parcel shape."""
    # Newest first on this backend; ties in the same minute keep that order.
    events = [e for e in raw.get("Tracking") or [] if isinstance(e, dict)]
    mapped = [(event, _map_event(event)) for event in events]

    newest = next(((e, s) for e, s in mapped if s is not ParcelStatus.UNKNOWN), None)
    if newest is not None:
        last_event, status = newest
    elif events:
        last_event, status = events[0], ParcelStatus.UNKNOWN
    else:
        last_event, status = {}, ParcelStatus.REGISTERED
    if status is ParcelStatus.IN_TRANSIT and any(e.get("Code") == "RT" for e in events):
        # The movement after a return scan is the trip back to the sender.
        status = ParcelStatus.RETURNING
    delivered = status is ParcelStatus.DELIVERED

    pickup = bool(raw.get("DeliveredInServicePoint")) or any(
        e.get("Code") in _PICKUP_CODES for e in events
    )
    service_point = raw.get("ServicePoint")
    service_point = service_point if isinstance(service_point, dict) else {}

    history: list[dict] | None = None
    if include_history:
        history = [
            {
                "timestamp": _timestamp(event),
                "status": event_status,
                "raw_status": _wording(event),
            }
            for event, event_status in reversed(mapped[:HISTORY_MAX_EVENTS])
        ]

    return {
        "carrier": "DHL",
        "barcode": raw.get("ExpeditionNumber"),
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": _wording(events[0]) if events else None,
        "delivered": delivered,
        "delivered_at": _timestamp(last_event) if delivered else None,
        "planned_from": None,
        "planned_to": None,
        "pickup": pickup,
        "pickup_point": (service_point.get("Name") or None) if pickup else None,
        "url": None,
        "weight": _weight(raw.get("Weight")),
        "dimensions": None,
        "history": history,
        "raw": raw,
    }
