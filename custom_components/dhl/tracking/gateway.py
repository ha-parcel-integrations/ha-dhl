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
    "LEG": ParcelStatus.REGISTERED,
    "UNDERWAY": ParcelStatus.IN_TRANSIT,
    "CUSTOMS": ParcelStatus.IN_TRANSIT,
    "IN_DELIVERY": ParcelStatus.OUT_FOR_DELIVERY,
    "PROBLEM": ParcelStatus.PROBLEM,
    "INTERVENTION": ParcelStatus.PROBLEM,
    "EXCEPTION": ParcelStatus.PROBLEM,
    "DELIVERED": ParcelStatus.DELIVERED,
}

# The finer `status` wins over `category` where it is known: the category
# cannot express at_pickup_point or returning, and INTERVENTION covers both a
# harmless reschedule and a cancelled delivery. Same ECOMMERCE vocabulary
# ha-dhl-nl maps, minus PARCEL_RETURNED_FROM_ROUTE and
# PARCEL_READY_FOR_RETURN_TO_HUB: on this gateway both appear in the normal
# outbound flow of a parcel dropped off at a ParcelShop, which then went on to
# be delivered.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "PRENOTIFICATION_RECEIVED": ParcelStatus.REGISTERED,
    "DATA_RECEIVED_WITH_PREFIX_LABEL": ParcelStatus.REGISTERED,
    "OUT_FOR_DELIVERY": ParcelStatus.OUT_FOR_DELIVERY,
    "LOAD_VEHICLE": ParcelStatus.OUT_FOR_DELIVERY,
    "PARCEL_INTO_FALLBACK": ParcelStatus.OUT_FOR_DELIVERY,
    "PARCEL_WILL_BE_DELIVERED_SOON": ParcelStatus.OUT_FOR_DELIVERY,
    "NOTIFICATION_FOR_PARCELSHOP_COLLECTION_HAS_BEEN_SENT": ParcelStatus.AT_PICKUP_POINT,
    "NOTIFICATION_FOR_PARCELSTATION_COLLECTION_HAS_BEEN_SENT": ParcelStatus.AT_PICKUP_POINT,
    "AWAITING_RECEIVER_COLLECTION": ParcelStatus.AT_PICKUP_POINT,
    "CLOSED_AWAITING_COLLECTION": ParcelStatus.AT_PICKUP_POINT,
    "DELIVERED_AT_ACCESSPOINT": ParcelStatus.AT_PICKUP_POINT,
    "DELIVERED_AT_PARCELSTATION": ParcelStatus.AT_PICKUP_POINT,
    "DELIVERY_CODE_MISSING_PICK_UP": ParcelStatus.AT_PICKUP_POINT,
    "ON_HOLD_FOR_COLLECTION": ParcelStatus.AT_PICKUP_POINT,
    "PARCEL_FOUND_AT_PARCELSHOP": ParcelStatus.AT_PICKUP_POINT,
    "PARCEL_HELD_FOR_COLLECTION_AT_LOCAL_DEPOT": ParcelStatus.AT_PICKUP_POINT,
    "REMINDER_FOR_COLLECTION_SENT_EMAIL": ParcelStatus.AT_PICKUP_POINT,
    "REMINDER_FOR_COLLECTION_SENT_LETTER": ParcelStatus.AT_PICKUP_POINT,
    "REMINDER_FOR_COLLECTION_SENT_SMS": ParcelStatus.AT_PICKUP_POINT,
    "INTERVENTION_RECEIVER_REQUESTS_DELIVERY_AT_ANOTHER_TIME/DATE": ParcelStatus.IN_TRANSIT,
    "INTERVENTION_RECEIVER_REQUESTS_DELIVERY_AT_ACCESSPOINT": ParcelStatus.IN_TRANSIT,
    "INTERVENTION_RECEIVER_REQUESTS_DELIVERY_AT_NEIGHBOURS": ParcelStatus.IN_TRANSIT,
    "INTERVENTION_RECEIVER_REQUESTS_DELIVERY_AT_PARCELSHOP": ParcelStatus.IN_TRANSIT,
    "INTERVENTION_RECEIVER_REQUESTS_DELIVERY_AT_PARCELSTATION": ParcelStatus.IN_TRANSIT,
    "INTERVENTION_RECEIVER_REQUESTS_DELIVERY_AT_PREFERRED_NEIGHBOURS": ParcelStatus.IN_TRANSIT,
    "PARCEL_ARRIVED_AT_LOCAL_DEPOT": ParcelStatus.IN_TRANSIT,
    "PARCEL_SORTED_AT_HUB": ParcelStatus.IN_TRANSIT,
    "PARCEL_PICKED_UP_AT_PARCELSHOP": ParcelStatus.IN_TRANSIT,
    "COLLECTED_AT_PARCELSHOP": ParcelStatus.DELIVERED,
    "COLLECTED_AT_ACCESSPOINT": ParcelStatus.DELIVERED,
    "COLLECTED_AT_PARCELSTATION": ParcelStatus.DELIVERED,
    "SHIPMENT_COLLECTED": ParcelStatus.DELIVERED,
    "DELIVERED_AT_NEIGHBOURS": ParcelStatus.DELIVERED,
    "DELIVERED_AT_PREFERED_NEIGHBOURS": ParcelStatus.DELIVERED,
    "DELIVERED_AT_SAFEPLACE": ParcelStatus.DELIVERED,
    "DELIVERED_IN_MAILBOX": ParcelStatus.DELIVERED,
    "DELIVERED_DAMAGED": ParcelStatus.DELIVERED,
    "DELIVERED_NOT_IN_TIME": ParcelStatus.DELIVERED,
    "DELIVERED_NO_CODE_VALIDATION": ParcelStatus.DELIVERED,
    "DELIVERED": ParcelStatus.DELIVERED,
    "ADDRESS_UNKNOWN": ParcelStatus.RETURNING,
    "CUSTOMS_DATA_INCORRECT_RETURN_TO_SHIPPER": ParcelStatus.RETURNING,
    "DAMAGE_RETURN": ParcelStatus.RETURNING,
    "DELIVERED_AT_SHIPPER": ParcelStatus.RETURNING,
    "DELIVERY_CODE_MISSING_RETURN": ParcelStatus.RETURNING,
    "DELIVERY_DATA_INCORRECT_RETURN": ParcelStatus.RETURNING,
    "EXPECTED_RETURN_DELIVERED_AT_SHIPPER_CALCULATED": ParcelStatus.RETURNING,
    "INTERVENTION_RECEIVER_REQUEST_DELIVERY_CANCELLED": ParcelStatus.RETURNING,
    "INTERVENTION_REQUEST_CANCEL_INTERVENTION": ParcelStatus.RETURNING,
    "INTERVENTION_REQUEST_CANCEL_INTERVENTION_SUSPECTED_FRAUD": ParcelStatus.RETURNING,
    "INTERVENTION_SHIPPER_REQUEST_DELIVERY_CANCELLED": ParcelStatus.RETURNING,
    "INVALID_SHIPMENT_SPECIFICATION_RETURN": ParcelStatus.RETURNING,
    "MISROUTED_RETURN_TO_SHIPPER": ParcelStatus.RETURNING,
    "NOT_HOME_RETURN_TO_SHIPPER": ParcelStatus.RETURNING,
    "NO_MONEY_RETURN": ParcelStatus.RETURNING,
    "ON_ROUTE_TO_SHIPPER": ParcelStatus.RETURNING,
    "PARCELSTATION_DELIVERY_UNSUCCESFULL_RETURN": ParcelStatus.RETURNING,
    "PARCEL_ALREADY_RETURNED": ParcelStatus.RETURNING,
    "PARCEL_RELABELED_FOR_RETURN_TO_SHIPPER": ParcelStatus.RETURNING,
    "PARCEL_SCANNED_AT_RETURN_HUB": ParcelStatus.RETURNING,
    "PARCEL_SCANNED_FOR_RETURN_TO_HUB": ParcelStatus.RETURNING,
    "PARCEL_TOO_HEAVY_RETURN": ParcelStatus.RETURNING,
    "PARCEL_TOO_LARGE_RETURN": ParcelStatus.RETURNING,
    "POSTAL_CODE_INCORRECT": ParcelStatus.RETURNING,
    "POSTPROCESS_DELIVERED_AT_SHIPPER": ParcelStatus.RETURNING,
    "POSTPROCESS_RETURN_CONSOLIDATION": ParcelStatus.RETURNING,
    "POSTPROCESS_RETURN_CONSOLIDATION_DEPART": ParcelStatus.RETURNING,
    "POSTPROCESS_RETURN_CONSOLIDATION_LOAD": ParcelStatus.RETURNING,
    "PO_BOX": ParcelStatus.RETURNING,
    "RECEIVER_RETURN": ParcelStatus.RETURNING,
    "RECEIVER_UNKNOWN_RETURN": ParcelStatus.RETURNING,
    "REFUSED_AT_PARCELSHOP": ParcelStatus.RETURNING,
    "REFUSED_BY_RECEIVER": ParcelStatus.RETURNING,
    "REFUSED_NOT_COLLECTED": ParcelStatus.RETURNING,
    "REFUSED_RETURN": ParcelStatus.RETURNING,
    "REFUSED_RETURN_TO_DD": ParcelStatus.RETURNING,
    "RETURNED_NOT_COLLECTED": ParcelStatus.RETURNING,
    "RETURNED_TO_SHIPPER": ParcelStatus.RETURNING,
    "RETURN_DELIVERED_AT_SHIPPER_CALCULATED": ParcelStatus.RETURNING,
    "SPONTANEOUS_RETURN": ParcelStatus.RETURNING,
    "STORAGE_PERIOD_ENDED_AT_ACCESSPOINT": ParcelStatus.RETURNING,
    "STORAGE_PERIOD_ENDED_AT_PARCELSHOP": ParcelStatus.RETURNING,
    "STORAGE_PERIOD_ENDED_AT_PARCELSTATION": ParcelStatus.RETURNING,
    "UNJUSTIFIED_SPONTANEOUS_RETURN": ParcelStatus.RETURNING,
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


def _map_event(event: dict) -> ParcelStatus:
    status = _STATUS_MAP.get(event.get("status"))
    if status is not None:
        return status
    category = event.get("category")
    status = _CATEGORY_MAP.get(category)
    if status is None:
        _warn_unmapped_category(str(category))
        return ParcelStatus.UNKNOWN
    return status


def _map_status(events: list[dict]) -> tuple[ParcelStatus, str | None]:
    if not events:
        return ParcelStatus.UNKNOWN, None
    last = events[-1]
    return _map_event(last), last.get("status")


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
                "status": _map_event(event),
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
