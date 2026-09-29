"""DHL Parcel Polska transport and canonical parcel mapping."""
from __future__ import annotations

import logging

import aiohttp

from ....const import NEW_ISSUE_URL, DHLApiError, DHLAuthError, ParcelStatus
from .session import DHLPlSession

_LOGGER = logging.getLogger(__name__)

# `status` — the raw TT_*/SP_* code (primary; tracking.md#status-the-raw-code-primary).
_RAW = {"TT_EDWP": ParcelStatus.REGISTERED, "SP_DSP": ParcelStatus.IN_TRANSIT, "TT_MAG": ParcelStatus.IN_TRANSIT, "TT_MAG_INT": ParcelStatus.IN_TRANSIT, "TT_PRZEKIERUJ": ParcelStatus.IN_TRANSIT, "TT_DWP": ParcelStatus.OUT_FOR_DELIVERY, "TT_DWP_INT": ParcelStatus.OUT_FOR_DELIVERY, "TT_DWP_PUNKT": ParcelStatus.OUT_FOR_DELIVERY, "TT_LK": ParcelStatus.AT_PICKUP_POINT, "TT_AWI": ParcelStatus.AT_PICKUP_POINT, "TT_OP": ParcelStatus.DELIVERED, "TT_DOR": ParcelStatus.DELIVERED, "TT_ZWN": ParcelStatus.RETURNING, "TT_DOR_ZWN": ParcelStatus.RETURNING, "TT_DELAY_KUR": ParcelStatus.PROBLEM, "TT_DELAY_MAG": ParcelStatus.PROBLEM, "TT_OWL": ParcelStatus.PROBLEM, "TT_CS": ParcelStatus.PROBLEM, "TT_ZGN": ParcelStatus.PROBLEM, "TT_LIK": ParcelStatus.PROBLEM, "SP_CN": ParcelStatus.PROBLEM, "ERR": ParcelStatus.PROBLEM}

# `menuTimelineLabel.status` — used only as a fallback for a raw code that isn't
# in `_RAW` yet. Which vocabulary it carries is contested: the 9-value coarse
# ladder, or the timeline names in `_TIMELINE`. The two overlap only on Route,
# Delivery and Delivered, which map the same way, so the fallback accepts both.
_LADDER = {
    "None": ParcelStatus.REGISTERED, "Resigned": ParcelStatus.PROBLEM,
    "Sent": ParcelStatus.IN_TRANSIT, "Route": ParcelStatus.IN_TRANSIT,
    "Delivery": ParcelStatus.OUT_FOR_DELIVERY, "ReturnToSender": ParcelStatus.RETURNING,
    "Delivered": ParcelStatus.DELIVERED, "DeliveredToSender": ParcelStatus.RETURNING,
    "Error": ParcelStatus.PROBLEM,
}

# DeliveredTo* means it reached the point or locker; Retrieved* is the collection.
_TIMELINE = {
    "ShipmentInPreparation": ParcelStatus.REGISTERED, "WaitingForCourierPickup": ParcelStatus.REGISTERED,
    "PostedAtPoint": ParcelStatus.IN_TRANSIT, "PickedUpByCourier": ParcelStatus.IN_TRANSIT,
    "Redirected": ParcelStatus.IN_TRANSIT, "RedirectedToPoint": ParcelStatus.IN_TRANSIT,
    "DeliveryToPoint": ParcelStatus.OUT_FOR_DELIVERY, "DeliveryToLocker": ParcelStatus.OUT_FOR_DELIVERY,
    "DeliveredToPoint": ParcelStatus.AT_PICKUP_POINT, "DeliveredToLocker": ParcelStatus.AT_PICKUP_POINT,
    "RetrievedFromPoint": ParcelStatus.DELIVERED, "RetrievedFromLocker": ParcelStatus.DELIVERED,
    "Refusal": ParcelStatus.RETURNING, "ParcelReturnsToSender": ParcelStatus.RETURNING,
    "ParcelReturnedToSender": ParcelStatus.RETURNING,
    "Resignated": ParcelStatus.PROBLEM, "Disposed": ParcelStatus.PROBLEM, "Lost": ParcelStatus.PROBLEM,
    "UnsuccessfulAttemptAtDelivery": ParcelStatus.PROBLEM, "SecondUnsuccessfulAttemptAtDelivery": ParcelStatus.PROBLEM,
    "DeliveryDelay": ParcelStatus.PROBLEM, "DeliveryProblem": ParcelStatus.PROBLEM,
    "WaitingForShipperDecision": ParcelStatus.PROBLEM, "ContactDHL": ParcelStatus.PROBLEM,
}


async def async_get_incoming(session: aiohttp.ClientSession, pl_session: DHLPlSession, device_id: str) -> list[dict]:
    """Fetch and merge the own and observed/shared inboxes.

    A 401/403 here means the session died despite a just-succeeded refresh
    (the refresh call cannot always detect this itself) — raise
    ``DHLAuthError`` so the coordinator starts reauth instead of retrying an
    inbox call that will never succeed.
    """
    token = await pl_session.async_refresh(device_id)
    headers = {"authorization": f"Bearer {token}", "content-type": "application/json"}
    async with session.post("https://mojdhl.pl/api/dhl/public/user/shipment/v2.1/list/incoming/active/1", json={"shipmentFilterTypes": [], "shipmentFilterStatuses": [], "page": 1}, headers=headers) as response:
        if response.status in (401, 403):
            raise DHLAuthError("Mój DHL inbox session expired")
        if response.status != 200:
            raise DHLApiError(f"Mój DHL inbox failed: HTTP {response.status}")
        body = await response.json(content_type=None)
    own = body.get("shipments", []) if isinstance(body, dict) else []
    async with session.post("https://mojdhl.pl/api/dhl/public/user/shipment/observed/v1.0/list/incoming/active", json={}, headers=headers) as response:
        if response.status in (401, 403):
            raise DHLAuthError("Mój DHL observed-list session expired")
        observed = await response.json(content_type=None) if response.status == 200 else []
    merged = {item.get("shipmentNumber"): item for item in observed if isinstance(item, dict)}
    merged.update({item.get("shipmentNumber"): item for item in own if isinstance(item, dict)})
    return [item for number, item in merged.items() if number]


_unmapped_status_logged: set[str] = set()


def _warn_unmapped_status(raw_status: str, ladder_status: str | None, status: ParcelStatus) -> None:
    # Warn even when the timeline rescues the status: only the raw code says
    # which new state DHL introduced.
    if raw_status in _unmapped_status_logged:
        return
    _unmapped_status_logged.add(raw_status)
    _LOGGER.warning(
        "Unrecognised DHL Parcel Polska status — help us map it. Open an "
        "issue and paste this line: %s\n"
        "  status=%s timeline=%s → reported as '%s'",
        NEW_ISSUE_URL,
        raw_status,
        ladder_status,
        status.value,
    )


def map_pl_status(raw_status: str | None, ladder_status: str | None) -> ParcelStatus:
    """Map a raw ``TT_*``/``SP_*`` code, falling back to the timeline step."""
    status = _RAW.get(raw_status or "") or _LADDER.get(ladder_status or "") or _TIMELINE.get(ladder_status or "", ParcelStatus.UNKNOWN)
    if raw_status and raw_status not in _RAW:
        _warn_unmapped_status(raw_status, ladder_status, status)
    return status


def normalize_parcel_pl(raw: dict, *, include_history: bool = False) -> dict:
    """Map one Mój DHL list item to the suite's canonical parcel shape."""
    timeline = raw.get("menuTimelineLabel") if isinstance(raw.get("menuTimelineLabel"), dict) else {}
    raw_status = raw.get("status") if isinstance(raw.get("status"), str) else None
    ladder_status = timeline.get("status") if isinstance(timeline.get("status"), str) else None
    status = map_pl_status(raw_status, ladder_status)
    timestamp = timeline.get("dateUtc") if isinstance(timeline.get("dateUtc"), str) else None
    return {"carrier": "DHL Parcel Polska", "barcode": raw.get("shipmentNumber"), "sender": raw.get("sender"),
            "receiver": None, "status": status, "raw_status": raw_status,
            "delivered": status is ParcelStatus.DELIVERED, "delivered_at": timestamp if status is ParcelStatus.DELIVERED else None,
            "planned_from": None, "planned_to": None,
            "pickup": status is ParcelStatus.AT_PICKUP_POINT, "pickup_point": None, "url": None,
            "weight": None, "dimensions": None, "history": None,
            "raw": raw}


def is_outgoing_element(raw: dict) -> bool:
    """Mój DHL's account inbox carries no outgoing shipments."""
    return False
