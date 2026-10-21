"""Per-backend ``normalize_parcel`` dispatcher, plus generic list helpers.

Mirrors ``account/parcels.py``'s role: the mapping logic itself lives in
``gateway.py``/``express.py``/``hamta.py``/``mojdhl.py``/``iberia.py``; this module dispatches on
which backend answered a given raw payload, combines the answers of several
keyless backends into one parcel, and carries the list helpers that are
identical regardless of which one produced it. Every raw payload carries a private
``_dhl_backend`` marker (stamped by the coordinator, stripped again in
``raw`` before publishing) so this dispatch never has to re-guess the shape.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DHL_TRACKING_PAGE_DEFAULT_LOCALE,
    DHL_TRACKING_PAGE_LANGUAGES,
    DHL_TRACKING_PAGE_URL,
    HISTORY_MAX_EVENTS,
)
from .express import normalize_parcel_express
from .gateway import normalize_parcel_gateway
from .hamta import normalize_parcel_hamta
from .iberia import normalize_parcel_iberia
from .mojdhl import normalize_parcel_mojdhl

BACKEND_KEY = "_dhl_backend"

_NORMALIZERS = {
    "gateway": normalize_parcel_gateway,
    "express": normalize_parcel_express,
    "hamta": normalize_parcel_hamta,
    "mojdhl": normalize_parcel_mojdhl,
    "iberia": normalize_parcel_iberia,
}

# Fields one backend may lack and another one fill in. Status, dates and the
# pickup point always come from the freshest answer as a whole.
_FILLABLE = ("sender", "receiver", "weight", "dimensions")

_OLDEST = datetime.min.replace(tzinfo=UTC)


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Dispatch to the right backend's ``normalize_parcel_<backend>``.

    ``raw`` must carry the ``_dhl_backend`` marker the coordinator stamps on
    every fetched/cached payload before this is called.
    """
    backend = raw[BACKEND_KEY]
    payload = {k: v for k, v in raw.items() if k != BACKEND_KEY}
    return _NORMALIZERS[backend](payload, include_history=include_history)


def _freshness(parcel: dict) -> datetime:
    moments = [parse_iso(e.get("timestamp")) for e in parcel.get("history") or []]
    moments.append(parse_iso(parcel.get("delivered_at")))
    return max((m for m in moments if m is not None), default=_OLDEST)


def merge_parcels(
    parcels: list[tuple[str, dict]], *, include_history: bool
) -> tuple[str, dict]:
    """Combine several backends' view of one code into a single parcel.

    ``parcels`` holds ``(backend, parcel)`` pairs in chain order, each
    normalised *with* history so its newest scan can be found. The backend
    with the newest scan decides the parcel; an earlier backend wins a tie.
    Returns that backend and the combined parcel.
    """
    backend, primary = max(
        enumerate(parcels), key=lambda item: (_freshness(item[1][1]), -item[0])
    )[1]
    merged = dict(primary)
    for field in _FILLABLE:
        if merged[field] is None:
            merged[field] = next(
                (p[field] for _, p in parcels if p[field] is not None), None
            )
    if not include_history:
        merged["history"] = None
    elif len(parcels) > 1:
        seen: set[tuple] = set()
        events: list[dict] = []
        for _, parcel in parcels:
            for event in parcel.get("history") or []:
                key = (event.get("timestamp"), event.get("raw_status"))
                if key not in seen:
                    seen.add(key)
                    events.append(event)
        events.sort(key=lambda e: parse_iso(e.get("timestamp")) or _OLDEST)
        merged["history"] = events[-HISTORY_MAX_EVENTS:]
    return backend, merged


def tracking_page_locale(hass: HomeAssistant) -> str:
    """Return the dhl.com locale for Home Assistant's country and language."""
    country = (hass.config.country or "").lower()
    if not country:
        return DHL_TRACKING_PAGE_DEFAULT_LOCALE
    language = (hass.config.language or "en").split("-")[0].lower()
    if language in DHL_TRACKING_PAGE_LANGUAGES.get(country, ()):
        return f"{country}-{language}"
    return f"{country}-en"


def tracking_page_url(code: str, locale: str) -> str:
    """Return the public dhl.com tracking page for one code."""
    return DHL_TRACKING_PAGE_URL.format(locale=locale, code=quote(code))


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
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
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
