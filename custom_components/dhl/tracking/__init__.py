"""Tracking-code source: routes each code to the backends that can answer it.

The keyless backends (the DHL Parcel gateway, Mój DHL's public lookup, DHL
eCommerce Iberia and DHL Freight Sweden) are cheap and unrationed, so every
one that accepts a code's shape is asked, and their answers are combined. The
Express app backend is not one of them. Its budget is scarce, and it has
never resolved a DHL Parcel barcode, so it is reached by the code's *shape*:
10-digit AWBs with a valid check digit, plus a code that matched no known
family and no keyless backend could answer
(dhl-nl/gateway_track_trace.md, dhl/express-app-backend.md).
"""

from __future__ import annotations

import re

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
BACKEND_MOJDHL = "mojdhl"
BACKEND_IBERIA = "iberia"
BACKEND_HAMTA = "hamta"
# Ten digits whose last is not the Express check digit: never an Express AWB,
# so it never spends the Express budget.
SHAPE_TEN_DIGIT = "ten_digit"
# Neither pattern matched: still tried on the keyless backends, and only then
# on the Express backend (see tracking/coordinator.py).
BACKEND_UNKNOWN = "unknown"


def express_check_digit_ok(code: str) -> bool:
    """Whether the last digit is the first nine modulo 7, as on every Express AWB."""
    return int(code[:9]) % 7 == int(code[9])


def classify_shape(code: str) -> str:
    """Return which backend a tracking code's shape routes it to."""
    if _BARCODE_RE.match(code):
        return BACKEND_GATEWAY
    if _EXPRESS_RE.match(code):
        return BACKEND_EXPRESS if express_check_digit_ok(code) else SHAPE_TEN_DIGIT
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
