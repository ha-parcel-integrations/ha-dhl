"""DHL Parcel Polska transport and canonical parcel mapping."""
from __future__ import annotations

import aiohttp

from ...const import DHLApiError, DHLAuthError, ParcelStatus
from .session import DHLPlSession

# `status` — the raw TT_*/SP_* code (primary; tracking.md#status-the-raw-code-primary).
_RAW = {"TT_EDWP": ParcelStatus.REGISTERED, "SP_DSP": ParcelStatus.IN_TRANSIT, "TT_MAG": ParcelStatus.IN_TRANSIT, "TT_MAG_INT": ParcelStatus.IN_TRANSIT, "TT_PRZEKIERUJ": ParcelStatus.IN_TRANSIT, "TT_DWP": ParcelStatus.OUT_FOR_DELIVERY, "TT_DWP_INT": ParcelStatus.OUT_FOR_DELIVERY, "TT_DWP_PUNKT": ParcelStatus.OUT_FOR_DELIVERY, "TT_LK": ParcelStatus.AT_PICKUP_POINT, "TT_AWI": ParcelStatus.AT_PICKUP_POINT, "TT_OP": ParcelStatus.DELIVERED, "TT_DOR": ParcelStatus.DELIVERED, "TT_ZWN": ParcelStatus.RETURNING, "TT_DOR_ZWN": ParcelStatus.RETURNING, "TT_DELAY_KUR": ParcelStatus.PROBLEM, "TT_DELAY_MAG": ParcelStatus.PROBLEM, "TT_OWL": ParcelStatus.PROBLEM, "TT_CS": ParcelStatus.PROBLEM, "TT_ZGN": ParcelStatus.PROBLEM, "TT_LIK": ParcelStatus.PROBLEM, "SP_CN": ParcelStatus.PROBLEM, "ERR": ParcelStatus.PROBLEM}

# `menuTimelineLabel.status` — the 9-value coarse ladder (tracking.md#menutimelinelabelstatus).
# Used only as a fallback for a raw code that isn't in `_RAW` yet — not the
# other way around. It is a *different, coarser* field than the 22-name
# `ShipmentStatusName` timeline (whose wire location was never confirmed by
# research): do not key this table on those names, e.g. "DeliveredToLocker".
_LADDER = {
    "None": ParcelStatus.REGISTERED, "Resigned": ParcelStatus.PROBLEM,
    "Sent": ParcelStatus.IN_TRANSIT, "Route": ParcelStatus.IN_TRANSIT,
    "Delivery": ParcelStatus.OUT_FOR_DELIVERY, "ReturnToSender": ParcelStatus.RETURNING,
    "Delivered": ParcelStatus.DELIVERED, "DeliveredToSender": ParcelStatus.RETURNING,
    "Error": ParcelStatus.PROBLEM,
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


def normalize_parcel_pl(raw: dict, *, include_history: bool = False) -> dict:
    """Map one Mój DHL list item to the suite's canonical parcel shape."""
    timeline = raw.get("menuTimelineLabel") if isinstance(raw.get("menuTimelineLabel"), dict) else {}
    raw_status = raw.get("status") if isinstance(raw.get("status"), str) else None
    ladder_status = timeline.get("status") if isinstance(timeline.get("status"), str) else None
    status = _RAW.get(raw_status or "") or _LADDER.get(ladder_status or "", ParcelStatus.UNKNOWN)
    timestamp = timeline.get("dateUtc") if isinstance(timeline.get("dateUtc"), str) else None
    return {"carrier": "DHL Parcel Polska", "barcode": raw.get("shipmentNumber"), "status": status,
            "raw_status": raw_status, "sender": raw.get("sender"), "receiver": None,
            "delivered": status is ParcelStatus.DELIVERED, "delivered_at": timestamp if status is ParcelStatus.DELIVERED else None,
            "planned_from": None, "planned_to": None, "weight": None, "dimensions": None,
            "pickup_point": None, "url": None, "history": None,
            "raw": {"package_type": raw.get("packageType"), "timeline_status": ladder_status}}


def is_outgoing_element(raw: dict) -> bool:
    """Mój DHL's account inbox carries no outgoing shipments."""
    return False
