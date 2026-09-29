"""Mój DHL's public by-number lookup (mojdhl.pl): fetch + normalize."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp

from ..account.countries.pl import map_pl_status
from ..account.countries.pl.session import solve_altcha
from ..const import (
    DHL_MOJDHL_MIN_CODE_LENGTH,
    DHL_PL_BASE_URL,
    DHL_PL_HEADERS,
    DHL_PL_REQUEST_TIMEOUT_SECONDS,
    DHLApiError,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=DHL_PL_REQUEST_TIMEOUT_SECONDS)


def mojdhl_shaped(code: str) -> bool:
    """Whether the backend's validator can accept ``code`` at all."""
    return len(code) >= DHL_MOJDHL_MIN_CODE_LENGTH


async def _json(
    session: aiohttp.ClientSession, method: str, path: str, **kwargs: Any
) -> tuple[int, Any]:
    try:
        async with session.request(
            method,
            f"{DHL_PL_BASE_URL}{path}",
            headers=DHL_PL_HEADERS,
            timeout=_TIMEOUT,
            **kwargs,
        ) as resp:
            try:
                return resp.status, await resp.json(content_type=None)
            except ValueError:
                return resp.status, None
    except aiohttp.ClientError as err:
        raise DHLApiError(str(err)) from err


async def _post_status(
    session: aiohttp.ClientSession, codes: list[str]
) -> tuple[int, Any]:
    # A solved challenge is single-use, so every request needs its own.
    status, challenge = await _json(session, "GET", "/auth/captcha/challenge")
    if status != 200 or not isinstance(challenge, dict):
        raise DHLApiError(f"Altcha challenge request failed: HTTP {status}")
    body: dict[str, str] = {
        f"number{index}": code for index, code in enumerate(codes, 1)
    }
    body["captcha-payload"] = solve_altcha(challenge)
    return await _json(session, "POST", "/shipment/status", json=body)


def _rejected(codes: list[str], body: Any) -> set[str]:
    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, dict):
        return set()
    return {
        codes[int(key[len("number"):]) - 1]
        for key in errors
        if key.startswith("number")
        and key[len("number"):].isdigit()
        and 0 < int(key[len("number"):]) <= len(codes)
    }


async def async_fetch_mojdhl(
    session: aiohttp.ClientSession, codes: list[str]
) -> dict[str, dict]:
    """Batch-fetch codes; return ``{shipmentNumber: shipment}``.

    An unknown number comes back with an empty ``shipments`` list, so codes
    the backend does not know are simply absent from the mapping. A number
    its validator rejects fails the whole batch with a 422 naming it; the
    batch is retried once without the rejected numbers.
    """
    codes = [c for c in codes if mojdhl_shaped(c)]
    if not codes:
        return {}
    status, body = await _post_status(session, codes)
    if status == 422:
        rejected = _rejected(codes, body)
        codes = [c for c in codes if c not in rejected]
        if not rejected or not codes:
            return {}
        status, body = await _post_status(session, codes)
    if status != 200:
        raise DHLApiError(f"HTTP {status}")
    if not isinstance(body, list):
        return {}
    results: dict[str, dict] = {}
    for item in body:
        if not isinstance(item, dict) or item.get("number") not in codes:
            continue
        shipments = [s for s in item.get("shipments") or [] if isinstance(s, dict)]
        if shipments:
            results[item["number"]] = shipments[0]
    return results


def _utc(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def normalize_parcel_mojdhl(raw: dict, *, include_history: bool = False) -> dict:
    """Map one Mój DHL public shipment onto the canonical parcel shape."""
    raw_status = raw.get("status") if isinstance(raw.get("status"), str) else None
    step = raw.get("timelineStep") if isinstance(raw.get("timelineStep"), str) else None
    status = map_pl_status(raw_status, step)
    delivered = status is ParcelStatus.DELIVERED

    return {
        "carrier": "DHL",
        "barcode": raw.get("shipmentNumber"),
        "sender": raw.get("sender") or None,
        "receiver": None,
        "status": status,
        "raw_status": raw.get("step") or raw_status,
        "delivered": delivered,
        "delivered_at": (
            _utc(raw.get("receiptDateUtc")) or _utc(raw.get("deliveryDateUtc"))
            if delivered
            else None
        ),
        "planned_from": None if delivered else _utc(raw.get("planOfDeliveryFromUtc")),
        "planned_to": None if delivered else _utc(raw.get("planOfDeliveryToUtc")),
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": None,
        "url": None,
        "weight": None,
        "dimensions": None,
        # The public lookup carries no event log.
        "history": None,
        "raw": raw,
    }
