"""DHL Germany: the account-inbox/by-number transport, status map, mapping.

The payload is confirmed against a real account, on the wire — but the
free-text status vocabulary and the delivery-window shape are still open, so
every contested field stays guarded with a one-shot ``WARNING`` rather than
assumed.

Needs its own nested package (rather than a flat ``countries/de.py``) because
it also needs an OIDC token-lifecycle module with no simpler-country
equivalent — see :mod:`.session`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp

from ...const import (
    DHL_DE_ARCHIVED_MARKER,
    DHL_DE_COOKIE_NAME,
    DHL_DE_PUBLIC_TRACKING_URL,
    DHL_DE_REQUEST_TIMEOUT_SECONDS,
    DHL_DE_TRACKING_HEADERS,
    DHL_DE_TRACKING_URL,
    HISTORY_MAX_EVENTS,
    NEW_ISSUE_URL,
    DHLApiError,
    DHLAuthError,
    ParcelStatus,
)
from .session import DHLDeAuthError, DHLDeSession, DHLDeSessionError

_LOGGER = logging.getLogger(__name__)

_BERLIN = ZoneInfo("Europe/Berlin")
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=DHL_DE_REQUEST_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# Transport — GET, dhli cookie, one 401 retry (BUILD_PLAN.md §3, §4)
# ---------------------------------------------------------------------------


_unexpected_body_logged = False


def _warn_unexpected_body_once(status: int, content_type: str | None, text: str) -> None:
    """One-shot: a 200 whose body isn't a JSON object at all.

    Distinct from the payload-shape WARNINGs in this module (those fire on a
    genuine ``sendungen`` element with an unrecognised key) — this fires
    before parsing even gets that far, most likely an HTML challenge/consent
    page rather than a shape change. Logs enough of the raw response to tell
    the two apart without needing a second round trip.
    """
    global _unexpected_body_logged
    if _unexpected_body_logged:
        return
    _unexpected_body_logged = True
    _LOGGER.warning(
        "DHL Germany's tracking endpoint answered HTTP %s with a non-JSON "
        "body (content-type=%s). Open an issue and paste this line: %s\n"
        "  body[:2000]=%r",
        status,
        content_type,
        NEW_ISSUE_URL,
        text[:2000],
    )


async def _async_do_request(
    session: aiohttp.ClientSession, id_token: str, params: dict[str, str]
) -> tuple[dict[str, Any] | None, int]:
    """One GET against the DE tracking endpoint; returns ``(body, status)``.

    Plain `dhli` cookie only — no `dhlcs`, no desktop-browser spoofing.
    """
    async with session.get(
        DHL_DE_TRACKING_URL,
        params=params,
        cookies={DHL_DE_COOKIE_NAME: id_token},
        headers=DHL_DE_TRACKING_HEADERS,
        timeout=_REQUEST_TIMEOUT,
    ) as response:
        try:
            body = await response.json(content_type=None)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            # Best-effort only — a broken diagnostic read must never take
            # down the actual request handling above.
            try:
                text = await response.text()
                content_type = response.content_type
            except Exception:
                text, content_type = repr(body), None
            _warn_unexpected_body_once(response.status, content_type, text)
        return body, response.status


async def _async_request(
    session: aiohttp.ClientSession,
    de_session: DHLDeSession,
    params: dict[str, str],
) -> dict[str, Any]:
    """Request with the session's ID token, refreshing once on a 401.

    Never loops: one retry, then whatever the retry returned is final
    (BUILD_PLAN.md §3's session.py contract). A rejected refresh token
    surfaces as :class:`~.session.DHLDeAuthError`, converted here to
    :class:`DHLAuthError` so the coordinator's existing auth handling (and
    HA's reauth flow) picks it up without knowing anything about OIDC.
    """
    try:
        id_token = await de_session.async_get_id_token()
        body, status = await _async_do_request(session, id_token, params)
        if status == 401:
            id_token = await de_session.async_handle_unauthorized()
            body, status = await _async_do_request(session, id_token, params)
    except DHLDeAuthError as err:
        raise DHLAuthError(str(err)) from err
    except DHLDeSessionError as err:
        raise DHLApiError(str(err)) from err

    if status != 200:
        raise DHLApiError(f"HTTP {status}")
    if not isinstance(body, dict):
        raise DHLApiError("unexpected body (not a JSON object)")
    return body


async def async_get_inbox_envelope(
    session: aiohttp.ClientSession, de_session: DHLDeSession
) -> dict[str, Any]:
    """Fetch the account inbox: ``{"sendungen": [...], "rateLimited": bool, ...}``.

    `piececode` omitted — confirmed live to behave as an inbox listing. An
    empty account returns ``sendungen: []``, never an error.
    """
    return await _async_request(
        session,
        de_session,
        {"noRedirect": "true", "language": "de", "cid": "app"},
    )


async def async_get_by_number_envelope(
    session: aiohttp.ClientSession, de_session: DHLDeSession, piece_code: str
) -> dict[str, Any]:
    """Fetch the same endpoint keyed by a specific tracking number (`track_parcel`)."""
    return await _async_request(
        session,
        de_session,
        {
            "piececode": piece_code,
            "noRedirect": "true",
            "language": "de",
            "cid": "app",
        },
    )


# ---------------------------------------------------------------------------
# Element selection — the inbox is not a flat list (BUILD_PLAN.md §5a, §5b)
# ---------------------------------------------------------------------------

_KNOWN_SENDUNGSLISTE_VALUES = {DHL_DE_ARCHIVED_MARKER, "AKTUELL"}
_sendungsliste_values_logged: set[str] = set()


def _warn_sendungsliste_value(value: str) -> None:
    """Log a `sendungsliste` value outside the confirmed vocabulary, once each."""
    if value in _KNOWN_SENDUNGSLISTE_VALUES or value in _sendungsliste_values_logged:
        return
    _sendungsliste_values_logged.add(value)
    _LOGGER.warning(
        "DHL Germany reported a new sendungsinfo.sendungsliste value — help "
        "us enumerate it. Open an issue and paste this line: %s\n  value=%s",
        NEW_ISSUE_URL,
        value,
    )


def select_active_elements(sendungen: list[Any]) -> list[dict]:
    """Drop archived elements from the inbox, falling back if that empties it.

    Single-source (one of three OSS clients), high cost if wrong (every
    parcel the user has ever received, surfaced forever), cheap to check —
    BUILD_PLAN.md §5b. Filters on ``sendungsinfo.sendungsliste`` (uppercased)
    equal to ``"ARCHIVIERT"``, the only value any source names.
    """
    elements = [item for item in sendungen if isinstance(item, dict)]
    survivors = []
    for element in elements:
        info = element.get("sendungsinfo")
        value = info.get("sendungsliste") if isinstance(info, dict) else None
        if isinstance(value, str) and value:
            _warn_sendungsliste_value(value.upper())
        if isinstance(value, str) and value.upper() == DHL_DE_ARCHIVED_MARKER:
            continue
        survivors.append(element)
    return survivors if survivors else elements


def find_element_by_id(elements: list[dict], piece_code: str) -> dict | None:
    """Match an element on ``.id``, falling back to the first one (§5b step 3)."""
    for element in elements:
        if element.get("id") == piece_code:
            return element
    return elements[0] if elements else None


def needs_enrichment(element: dict) -> bool:
    """Whether an inbox element is a bare stub the account listing hasn't detailed yet.

    A stub has no `sendungsverlauf` at all and needs a by-number fetch to
    fill one in — an element already carrying a `sendungNichtGefunden`
    marker is left alone either way, since :func:`is_not_found` decides its
    fate from `sendungsverlauf` alone.
    """
    if isinstance(element.get("sendungNichtGefunden"), dict):
        return False
    details = element.get("sendungsdetails")
    if not isinstance(details, dict) or isinstance(
        details.get("sendungNichtGefunden"), dict
    ):
        return False
    return not isinstance(details.get("sendungsverlauf"), dict)


def is_not_found(element: dict) -> bool:
    """Whether a populated element is *not* a real parcel.

    The ``sendungNichtGefunden.keineDatenVerfuegbar`` marker also appears on
    real, account-linked parcels that simply have no scan events yet —
    confirmed live: DHL tags every zero-event shipment this way, not only
    genuinely unrecognised piece codes, so it is not a reliable signal on
    its own. A ``sendungsverlauf`` dict being present at all — even with
    zero events — means the parcel is real; its absence means there is
    nothing to show, marker or not.
    """
    details = element.get("sendungsdetails")
    verlauf = details.get("sendungsverlauf") if isinstance(details, dict) else None
    return not isinstance(verlauf, dict)


# ---------------------------------------------------------------------------
# Status mapping — the fortschritt ladder, rung 2 disputed (BUILD_PLAN.md §6)
# ---------------------------------------------------------------------------

_LADDER: dict[int, ParcelStatus] = {
    0: ParcelStatus.REGISTERED,
    1: ParcelStatus.REGISTERED,
    # Rung 2 is genuinely contested between two third-party sources
    # (registered vs in_transit). Shipped conservative per BUILD_PLAN.md §6:
    # reporting a parcel as moving when it has not is the worse error.
    2: ParcelStatus.REGISTERED,
    3: ParcelStatus.IN_TRANSIT,
    4: ParcelStatus.OUT_FOR_DELIVERY,
    5: ParcelStatus.DELIVERED,
}

_unmapped_fortschritt_logged: set[int] = set()
_rung_two_logged = False
_maximal_fortschritt_logged = False


def _warn_unmapped_fortschritt(value: int, maximal: int) -> None:
    if value in _unmapped_fortschritt_logged:
        return
    _unmapped_fortschritt_logged.add(value)
    _LOGGER.warning(
        "Unrecognised DHL Germany fortschritt value — help us map it. Open "
        "an issue and paste this line: %s\n"
        "  fortschritt=%s maximalFortschritt=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        value,
        maximal,
    )


def _warn_rung_two(raw_status: str | None) -> None:
    """One-shot: the first parcel observed at fortschritt==2 (§7a) — settles the dispute."""
    global _rung_two_logged
    if _rung_two_logged:
        return
    _rung_two_logged = True
    _LOGGER.warning(
        "DHL Germany parcel seen at fortschritt=2 for the first time — two "
        "third-party sources disagree on what this means (registered vs "
        "in transit). Please tell us what the DHL app showed. Open an "
        "issue and paste this line: %s\n  raw_status=%r",
        NEW_ISSUE_URL,
        raw_status,
    )


def _warn_unexpected_maximal_fortschritt(value: int) -> None:
    global _maximal_fortschritt_logged
    if _maximal_fortschritt_logged:
        return
    _maximal_fortschritt_logged = True
    _LOGGER.warning(
        "DHL Germany reported maximalFortschritt=%s (expected 5) — the "
        "progress ladder may have changed. Open an issue: %s",
        value,
        NEW_ISSUE_URL,
    )


def map_parcel_status_de(
    fortschritt: Any, maximal_fortschritt: Any, *, raw_status: str | None = None
) -> ParcelStatus:
    """Map ``sendungsverlauf.fortschritt`` to a canonical status.

    Bounded by ``maximalFortschritt`` rather than a literal ``5`` — all three
    reconstruction sources read it, and one defends against it being absent
    or non-positive by substituting 5, which is the behaviour copied here.
    ``raw_status`` is only used to enrich the rung-2 one-shot WARNING.
    """
    try:
        maximal = int(maximal_fortschritt)
        if maximal <= 0:
            raise ValueError
    except (TypeError, ValueError):
        maximal = 5
    if maximal != 5:
        _warn_unexpected_maximal_fortschritt(maximal)

    try:
        value = int(fortschritt)
    except (TypeError, ValueError):
        return ParcelStatus.UNKNOWN

    if value == 2:
        _warn_rung_two(raw_status)

    if value == maximal:
        return ParcelStatus.DELIVERED
    mapped = _LADDER.get(value)
    if mapped is not None and 0 <= value <= maximal:
        return mapped
    _warn_unmapped_fortschritt(value, maximal)
    return ParcelStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Payload mapping (BUILD_PLAN.md §5)
# ---------------------------------------------------------------------------

_KNOWN_SENDUNGSDETAILS_KEYS = {
    "sendungsverlauf",
    "istZugestellt",
    "zielland",
    "retoure",
    "ruecksendung",
    "kleinpaket",
    "expressSendung",
    "quelle",
    "zustellung",
    # Confirmed real, raw-only fields — not unknowns. `email` is PII,
    # redacted in diagnostics.py's TO_REDACT.
    "sendungsnummern",
    "services",
    "isSameDayDelivery",
    "bahnpaket",
    "mehrInformationenVerfuegbar",
    "international",
    "showDigitalNotificationCtaHint",
    "nachhaltigkeitsstatus",
    "unplausibel",
    "invalidTimeOfDay",
    "email",
    "isShipperPlz",
    "showQualityLevelHint",
    "twoManHandling",
}

_unexpected_keys_logged: set[str] = set()
_delivered_conflict_logged: set[str] = set()
_raw_status_kurz_status_logged = False
_delivery_window_shape_logged = False
_timestamp_parse_failed_logged = False


def _warn_unexpected_sendungsdetails_keys(details: dict) -> None:
    """One-shot-per-key: an unrecognised sendungsdetails key (§7a) — likely the pickup-point field."""
    for key, value in details.items():
        if key in _KNOWN_SENDUNGSDETAILS_KEYS or key in _unexpected_keys_logged:
            continue
        _unexpected_keys_logged.add(key)
        _LOGGER.warning(
            "DHL Germany's sendungsdetails has an unrecognised field — this "
            "is the most likely place a pickup-point field is hiding. Open "
            "an issue and paste this line: %s\n  %s: %s",
            NEW_ISSUE_URL,
            key,
            type(value).__name__,
        )


def _warn_delivered_conflict(barcode: str | None) -> None:
    key = barcode or "<unknown>"
    if key in _delivered_conflict_logged:
        return
    _delivered_conflict_logged.add(key)
    _LOGGER.warning(
        "DHL Germany parcel %s: istZugestellt disagrees with "
        "fortschritt >= maximalFortschritt. Please tell us which one was "
        "right. Open an issue: %s",
        key,
        NEW_ISSUE_URL,
    )


def _warn_raw_status_kurz_status_once(has_status: bool) -> None:
    global _raw_status_kurz_status_logged
    if _raw_status_kurz_status_logged:
        return
    _raw_status_kurz_status_logged = True
    _LOGGER.warning(
        "DHL Germany response carried kurzStatus for the first time "
        "(sendungsverlauf.status also present: %s). Open an issue so we can "
        "settle which is the better raw_status: %s",
        has_status,
        NEW_ISSUE_URL,
    )


def _warn_delivery_window_shape_once(keys_present: list[str]) -> None:
    global _delivery_window_shape_logged
    if _delivery_window_shape_logged:
        return
    _delivery_window_shape_logged = True
    _LOGGER.warning(
        "DHL Germany response carried a delivery-window field for the first "
        "time. Open an issue and paste this line: %s\n  keys=%s",
        NEW_ISSUE_URL,
        keys_present,
    )


def _warn_timestamp_parse_failed_once(value: Any) -> None:
    global _timestamp_parse_failed_logged
    if _timestamp_parse_failed_logged:
        return
    _timestamp_parse_failed_logged = True
    _LOGGER.warning(
        "DHL Germany returned a timestamp we could not parse (assuming "
        "Europe/Berlin for naive values elsewhere). Open an issue and paste "
        "this line: %s\n  value=%r",
        NEW_ISSUE_URL,
        value,
    )


def _parse_de_timestamp(value: Any) -> str | None:
    """Parse a DHL DE timestamp permissively; naive values assume Europe/Berlin.

    No source ever supplied an example (BUILD_PLAN.md §5), so this accepts
    ISO 8601 with or without an offset/`Z`, and logs+gives up on anything
    else rather than crashing.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        _warn_timestamp_parse_failed_once(value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_BERLIN)
    return parsed.astimezone(timezone.utc).isoformat()


def _build_history_de(
    events: list[Any], *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build canonical ``history`` from ``sendungsverlauf.events[]``.

    Sorted defensively — the three reconstruction sources hold *incompatible*
    assumptions about wire order (BUILD_PLAN.md §5), so nothing here relies
    on it. ``status`` stays ``None`` on every entry: the free-text
    ``events[].status`` is an open, localised vocabulary no source
    enumerates, and BUILD_PLAN.md §6 is explicit that it must never be keyed
    off for logic.
    """
    parseable: list[tuple[datetime, dict]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        timestamp = _parse_de_timestamp(event.get("datum"))
        if timestamp is None:
            continue
        parseable.append(
            (
                datetime.fromisoformat(timestamp),
                {
                    "timestamp": timestamp,
                    "status": None,
                    "raw_status": event.get("status"),
                },
            )
        )
    parseable.sort(key=lambda item: item[0])
    return [entry for _, entry in parseable][-max_events:]


def _delivery_window(zustellung: dict) -> tuple[str | None, str | None]:
    """Probe all four contested delivery-window keys (BUILD_PLAN.md §5).

    Prefers the ``Von``/``Bis`` pair when present (canonical wants two
    timestamps, not a display string), falling back to the singular
    ``zustellzeitfenster`` with ``zustelldatum`` as the ETA fallback.
    """
    von = zustellung.get("zustellzeitfensterVon")
    bis = zustellung.get("zustellzeitfensterBis")
    fenster = zustellung.get("zustellzeitfenster")
    datum = zustellung.get("zustelldatum")

    present = [
        key
        for key, value in (
            ("zustellzeitfensterVon", von),
            ("zustellzeitfensterBis", bis),
            ("zustellzeitfenster", fenster),
            ("zustelldatum", datum),
        )
        if value
    ]
    if present:
        _warn_delivery_window_shape_once(present)

    if von or bis:
        return _parse_de_timestamp(von), _parse_de_timestamp(bis)
    if fenster:
        return _parse_de_timestamp(fenster) or fenster, None
    if datum:
        return _parse_de_timestamp(datum), None
    return None, None


def normalize_parcel_de(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict for one ``sendungen`` element.

    Payload confirmed against a real account; every key is still guarded
    since a genuinely unrecognised field remains possible on other accounts.
    """
    barcode = raw.get("id")

    details = raw.get("sendungsdetails")
    details = details if isinstance(details, dict) else {}
    _warn_unexpected_sendungsdetails_keys(details)

    verlauf = details.get("sendungsverlauf")
    verlauf = verlauf if isinstance(verlauf, dict) else {}

    fortschritt = verlauf.get("fortschritt")
    maximal_fortschritt = verlauf.get("maximalFortschritt")

    # Contested: raw_status prefers sendungsverlauf.status (2/3 sources);
    # kurzStatus is logged but not used as the primary text (§5).
    raw_status = verlauf.get("status") or None
    kurz_status = verlauf.get("kurzStatus")
    if kurz_status:
        _warn_raw_status_kurz_status_once(bool(raw_status))
    if raw_status is None:
        raw_status = kurz_status

    status = map_parcel_status_de(fortschritt, maximal_fortschritt, raw_status=raw_status)

    # Contested: two sources read istZugestellt, one derives from the
    # ladder. Read the flag when present, else derive; warn once per parcel
    # if they ever disagree (BUILD_PLAN.md §5/§7a).
    delivered_flag = details.get("istZugestellt")
    try:
        derived_delivered = int(fortschritt) >= int(maximal_fortschritt or 5)
    except (TypeError, ValueError):
        derived_delivered = status is ParcelStatus.DELIVERED
    if isinstance(delivered_flag, bool):
        delivered = delivered_flag
        if delivered_flag != derived_delivered:
            _warn_delivered_conflict(barcode)
    else:
        delivered = derived_delivered

    delivered_at = (
        _parse_de_timestamp(verlauf.get("datumAktuellerStatus")) if delivered else None
    )

    events = verlauf.get("events")
    events = events if isinstance(events, list) else []
    history = _build_history_de(events) if include_history else None

    zustellung = details.get("zustellung")
    zustellung = zustellung if isinstance(zustellung, dict) else {}
    planned_from, planned_to = (None, None) if delivered else _delivery_window(zustellung)

    # Confirmed live: shipment-level `retoure`/`ruecksendung` flags, distinct
    # from the per-event `events[].ruecksendung` — ORed since either can be
    # the one actually set.
    retoure = details.get("retoure")
    ruecksendung = details.get("ruecksendung")
    if bool(retoure) or bool(ruecksendung):
        status = ParcelStatus.RETURNING

    tracking_url = (
        f"{DHL_DE_PUBLIC_TRACKING_URL}?piececode={quote(barcode)}"
        if barcode
        else DHL_DE_PUBLIC_TRACKING_URL
    )

    return {
        "carrier": "DHL",
        "barcode": barcode,
        # `.zustellung.empfaenger.name` is "who took the parcel", not
        # necessarily the addressee (Packstation/Filiale/neighbour) — single
        # source, so it stays out of `receiver` until a tester export
        # confirms the semantics (BUILD_PLAN.md §5). It is still present,
        # redacted, inside `raw`.
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": raw_status,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
        # No source names a Packstation/Filiale field — the largest known gap
        # (BUILD_PLAN.md §6/§7c).
        "pickup_point": None,
        "url": tracking_url,
        "weight": None,
        "dimensions": None,
        "history": history,
        "raw": raw,
    }


__all__ = [
    "async_get_by_number_envelope",
    "async_get_inbox_envelope",
    "find_element_by_id",
    "is_not_found",
    "needs_enrichment",
    "map_parcel_status_de",
    "normalize_parcel_de",
    "select_active_elements",
]
