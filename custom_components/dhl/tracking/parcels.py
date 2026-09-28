"""Per-backend ``normalize_parcel`` dispatcher, plus generic list helpers.

Mirrors ``account/parcels.py``'s role: the mapping logic itself lives in
``gateway.py``/``express.py``; this module dispatches on which backend
answered a given raw payload and carries the list helpers that are identical
regardless of which one produced it. Every raw payload carries a private
``_dhl_backend`` marker (stamped by the coordinator, stripped again in
``raw`` before publishing) so this dispatch never has to re-guess the shape.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry

from ..const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
)
from .express import normalize_parcel_express
from .gateway import normalize_parcel_gateway

BACKEND_KEY = "_dhl_backend"

_NORMALIZERS = {
    "gateway": normalize_parcel_gateway,
    "express": normalize_parcel_express,
}


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Dispatch to the right backend's ``normalize_parcel_<backend>``.

    ``raw`` must carry the ``_dhl_backend`` marker the coordinator stamps on
    every fetched/cached payload before this is called.
    """
    backend = raw[BACKEND_KEY]
    payload = {k: v for k, v in raw.items() if k != BACKEND_KEY}
    return _NORMALIZERS[backend](payload, include_history=include_history)


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    Identical contract to ``account/parcels.py``'s: parcels with a missing or
    unparseable value always sort to the end.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
