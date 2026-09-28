"""The keyless api-gw.dhlparcel.nl gateway: fetch + normalize.

No login, no key, no per-code request — the whole gateway-routed subset of a
poll's tracked codes goes out as one batched, comma-separated request. Unknown
barcodes are dropped silently from a mixed batch rather than erred, so a
result is matched on the returned ``barcode`` field, never on array position.
"""
from __future__ import annotations

import logging

import aiohttp

from ..const import (
    DHL_GATEWAY_HEADERS,
    DHL_GATEWAY_REQUEST_TIMEOUT_SECONDS,
    DHL_GATEWAY_URL,
    HISTORY_MAX_EVENTS,
    NEW_ISSUE_URL,
    DHLApiError,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=DHL_GATEWAY_REQUEST_TIMEOUT_SECONDS)

# Map on the coarse, network-independent `category`; PROBLEM is a transient
# event category, not a terminal state, so it is refined below by `status`
# rather than trusted on its own. RETURNED_TO_SHIPPER is a fine `status`
# value under any category — a return shipment's log has been observed to
# resume with further UNDERWAY events after it, so it is checked on whichever
# event is last, not assumed to end the log.
_CATEGORY_MAP: dict[str, ParcelStatus] = {
    "DATA_RECEIVED": ParcelStatus.REGISTERED,
    "UNDERWAY": ParcelStatus.IN_TRANSIT,
    "IN_DELIVERY": ParcelStatus.OUT_FOR_DELIVERY,
    "PROBLEM": ParcelStatus.PROBLEM,
    "DELIVERED": ParcelStatus.DELIVERED,
}

_warned_categories: set[str] = set()


class DHLGatewayError(DHLApiError):
    """Raised when the gateway request itself fails (transport, not 404)."""


async def async_fetch_gateway(
    session: aiohttp.ClientSession, codes: list[str]
) -> dict[str, dict]:
    """Batch-fetch every gateway-routed code; return ``{barcode: raw}``.

    A 404 (plain text, never JSON) means none of the batch resolved — not an
    error. Codes the gateway does not recognise are simply absent from the
    returned mapping, same as a 404 for the caller's purposes.
    """
    if not codes:
        return {}
    try:
        async with session.get(
            DHL_GATEWAY_URL,
            params={"key": ",".join(codes)},
            headers=DHL_GATEWAY_HEADERS,
            timeout=_TIMEOUT,
        ) as resp:
            if resp.status == 404:
                return {}
            if resp.status != 200:
                raise DHLGatewayError(f"HTTP {resp.status}")
            data = await resp.json(content_type=None)
    except aiohttp.ClientError as err:
        raise DHLGatewayError(str(err)) from err
    if not isinstance(data, list):
        return {}
    return {
        item["barcode"]: item
        for item in data
        if isinstance(item, dict) and item.get("barcode")
    }


def _warn_unmapped_category(category: str) -> None:
    if category in _warned_categories:
        return
    _warned_categories.add(category)
    _LOGGER.warning(
        "DHL tracking gateway reported an unrecognised event category %r — "
        "mapped to 'unknown'. Please report this: %s",
        category,
        NEW_ISSUE_URL,
    )


def _map_status(events: list[dict]) -> tuple[ParcelStatus, str | None]:
    if not events:
        return ParcelStatus.UNKNOWN, None
    last = events[-1]
    if last.get("status") == "RETURNED_TO_SHIPPER":
        return ParcelStatus.RETURNING, last.get("status")
    category = last.get("category")
    status = _CATEGORY_MAP.get(category)
    if status is None:
        _warn_unmapped_category(str(category))
        status = ParcelStatus.UNKNOWN
    return status, last.get("status")


def normalize_parcel_gateway(raw: dict, *, include_history: bool = False) -> dict:
    """Map one gateway response object onto the canonical parcel shape."""
    events = raw.get("events") or []
    status, raw_status = _map_status(events)
    last = events[-1] if events else {}

    history: list[dict] | None = None
    if include_history:
        # Already oldest-first on this backend — do not reverse (unlike the
        # Express feed, which is newest-first).
        history = [
            {
                "timestamp": event.get("timestamp"),
                "status": None,
                "raw_status": event.get("status"),
            }
            for event in events[-HISTORY_MAX_EVENTS:]
        ]

    return {
        "carrier": "DHL",
        "barcode": raw.get("barcode"),
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": raw_status,
        "delivered": bool(raw.get("deliveredAt")),
        "delivered_at": raw.get("deliveredAt"),
        "planned_from": last.get("momentIndication"),
        "planned_to": None,
        "pickup": False,
        "pickup_point": None,
        "url": None,
        "weight": None,
        "dimensions": None,
        "history": history,
        "raw": raw,
    }
