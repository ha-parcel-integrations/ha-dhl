"""Tracking-code source: routes each code to the backend that can answer it.

Two backends answer disjoint code shapes (dhl-nl/gateway_track_trace.md,
dhl/express-app-backend.md): the keyless gateway resolves DHL Parcel barcode
families, the Express app backend resolves bare 10-digit AWBs, and neither
has ever resolved the other's shape. Routing is therefore a classification of
the *code*, not a fallback chain tried against both backends — trying a
confidently-classified code against the wrong backend only wastes the
Express backend's scarce budget for a request that can never resolve.
"""
from __future__ import annotations

import re
from datetime import datetime

from ..const import (
    CONF_DIRECTION,
    DEFAULT_DIRECTION,
    DHL_EXPRESS_AWB_PATTERN,
    DHL_GATEWAY_BARCODE_PATTERNS,
)

_BARCODE_RE = re.compile("|".join(f"(?:{p})" for p in DHL_GATEWAY_BARCODE_PATTERNS))
_EXPRESS_RE = re.compile(DHL_EXPRESS_AWB_PATTERN)

BACKEND_GATEWAY = "gateway"
BACKEND_EXPRESS = "express"
# Neither pattern matched: still tried against the gateway (keyless,
# unthrottled, cheap) per the plan's one narrow, deliberately-inferred rule —
# never against the Express backend directly, only as that gateway attempt's
# fallback (see tracking/coordinator.py).
BACKEND_UNKNOWN = "unknown"


def classify_shape(code: str) -> str:
    """Return which backend a tracking code's shape routes it to."""
    if _BARCODE_RE.match(code):
        return BACKEND_GATEWAY
    if _EXPRESS_RE.match(code):
        return BACKEND_EXPRESS
    return BACKEND_UNKNOWN


def normalize_tracking_code(value: str) -> str:
    """Upper-case and strip a pasted code.

    The gateway is case-sensitive (``lx200352688de`` 404s, ``LX200352688DE``
    resolves) — upper-casing is always safe for every shape family, including
    the digit-only Express AWBs this affects not at all.
    """
    return (value or "").strip().upper()


def valid_tracking_code(value: str) -> bool:
    """Whether ``value`` is worth trying at all.

    Deliberately not a shape allowlist: the plan is explicit that an unknown
    shape must still be tried rather than rejected at the door, since the
    gateway's own scope edge is not fully mapped.
    """
    return bool(value)


def tracked_direction(item: dict) -> str:
    """Return the declared direction of one tracked-parcel options entry."""
    return item.get(CONF_DIRECTION) or DEFAULT_DIRECTION


def end_of_day(moment: str) -> str | None:
    """Return 23:59:59 on ``moment``'s own day, keeping its UTC offset."""
    try:
        parsed = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
