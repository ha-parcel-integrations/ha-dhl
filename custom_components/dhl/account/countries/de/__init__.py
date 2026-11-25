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

from datetime import UTC, datetime, time
import html
import logging
import re
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp

from ....const import (
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
from ....delivery_window import is_pickup, planned_for_status, planned_window
from .session import DHLDeAuthError, DHLDeSession, DHLDeSessionError

_LOGGER = logging.getLogger(__name__)

_BERLIN = ZoneInfo("Europe/Berlin")
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=DHL_DE_REQUEST_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# Transport — GET, dhli cookie, one 401 retry
# ---------------------------------------------------------------------------


_unexpected_body_logged = False


def _warn_unexpected_body_once(
    status: int, content_type: str | None, text: str
) -> None:
    """One-shot: a 200 whose body isn't a JSON object at all.

    Distinct from the payload-shape WARNINGs in this module (those fire on a
    genuine ``sendungen`` element with an unrecognised key) — this fires
    before parsing even gets that far, most likely an HTML challenge/consent
    page rather than a shape change. Logs enough of the raw response to tell
    the two apart without needing a second round trip.
    """
    global _unexpected_body_logged  # noqa: PLW0603 - one-shot warning flag, reset by tests
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
            except Exception:  # noqa: BLE001 - diagnostics must never break the request
                text, content_type = repr(body), None
            _warn_unexpected_body_once(response.status, content_type, text)
        return body, response.status


async def _async_request(
    session: aiohttp.ClientSession,
    de_session: DHLDeSession,
    params: dict[str, str],
) -> dict[str, Any]:
    """Request with the session's ID token, refreshing once on a 401.

    Never loops: one retry, then whatever the retry returned is final. A
    rejected refresh token surfaces as :class:`~.session.DHLDeAuthError`,
    converted here to :class:`DHLAuthError` so the coordinator's existing
    auth handling (and
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
# Element selection — the inbox is not a flat list
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
    parcel the user has ever received, surfaced forever), cheap to check.
    Filters on ``sendungsinfo.sendungsliste`` (uppercased)
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
    return survivors or elements


def find_element_by_id(elements: list[dict], piece_code: str) -> dict | None:
    """Match an element on ``.id``, falling back to the first one."""
    for element in elements:
        if element.get("id") == piece_code:
            return element
    return elements[0] if elements else None


def needs_enrichment(element: dict) -> bool:
    """Whether an inbox element is a bare stub the account listing hasn't detailed yet.

    Distinct from :func:`is_not_found`: a genuine not-found element still
    carries a `sendungsverlauf` (with zero progress) alongside its
    `sendungNichtGefunden` marker — a stub has neither and needs a
    by-number fetch to fill in.
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
    """Whether a populated element is *not* a real parcel — three failure modes.

    Checks the explicit ``sendungNichtGefunden.keineDatenVerfuegbar`` marker
    first, then falls back to the structural test (no events, no
    ``kurzStatus``) a second independent client uses for the same condition.
    """
    not_found = element.get("sendungNichtGefunden")
    if isinstance(not_found, dict) and not_found.get("keineDatenVerfuegbar"):
        return True
    details = element.get("sendungsdetails")
    verlauf = details.get("sendungsverlauf") if isinstance(details, dict) else None
    if not isinstance(verlauf, dict):
        return True
    events = verlauf.get("events")
    has_events = isinstance(events, list) and len(events) > 0
    has_kurz_status = bool(verlauf.get("kurzStatus"))
    return not has_events and not has_kurz_status


# ---------------------------------------------------------------------------
# Status mapping — the fortschritt ladder
# ---------------------------------------------------------------------------

_LADDER: dict[int, ParcelStatus] = {
    0: ParcelStatus.REGISTERED,
    1: ParcelStatus.REGISTERED,
    # Rung 2 ("Im Zustellzentrum") was contested between two third-party
    # sources (registered vs in_transit); a third source settled it. Mapped
    # IN_TRANSIT for consistency with ha-dhl-nl, where an equivalent
    # depot/hub scan (PARCEL_ARRIVED_AT_LOCAL_DEPOT) also maps IN_TRANSIT —
    # REGISTERED is reserved for before the carrier has physically scanned
    # the parcel at all.
    2: ParcelStatus.IN_TRANSIT,
    3: ParcelStatus.IN_TRANSIT,
    4: ParcelStatus.OUT_FOR_DELIVERY,
    5: ParcelStatus.DELIVERED,
}

_unmapped_fortschritt_logged: set[int] = set()
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


def _warn_unexpected_maximal_fortschritt(value: int) -> None:
    global _maximal_fortschritt_logged  # noqa: PLW0603 - one-shot warning flag, reset by tests
    if _maximal_fortschritt_logged:
        return
    _maximal_fortschritt_logged = True
    _LOGGER.warning(
        "DHL Germany reported maximalFortschritt=%s (expected 5) — the "
        "progress ladder may have changed. Open an issue: %s",
        value,
        NEW_ISSUE_URL,
    )


def map_parcel_status_de(fortschritt: Any, maximal_fortschritt: Any) -> ParcelStatus:
    """Map ``sendungsverlauf.fortschritt`` to a canonical status.

    Bounded by ``maximalFortschritt`` rather than a literal ``5`` — all three
    reconstruction sources read it, and one defends against it being absent
    or non-positive by substituting 5, which is the behaviour copied here.
    """
    try:
        maximal = int(maximal_fortschritt)
    except (TypeError, ValueError):
        maximal = 5
    else:
        if maximal <= 0:
            maximal = 5
    if maximal != 5:
        _warn_unexpected_maximal_fortschritt(maximal)

    try:
        value = int(fortschritt)
    except (TypeError, ValueError):
        return ParcelStatus.UNKNOWN

    if value == maximal:
        return ParcelStatus.DELIVERED
    mapped = _LADDER.get(value)
    if mapped is not None and 0 <= value <= maximal:
        return mapped
    _warn_unmapped_fortschritt(value, maximal)
    return ParcelStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Payload mapping
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
    # Reported live on a reporter's account (issue #18). `isHeavyPackage` is a
    # flag with no weight behind it, so it has no canonical home;
    # `panEmpfaenger` is the recipient block below.
    "isHeavyPackage",
    "panEmpfaenger",
    # Reported live on a smart-Packstation parcel (issue #7). Neither has a
    # canonical home; `liveTracking` is redacted in diagnostics since a live
    # van position sits near the recipient's address.
    "isNoPackstation",
    "liveTracking",
    # Reported live on a reporter's account (issue #26). `versender` is the
    # shipper block, read as the `sender` fallback below. `oneTimeCode` is a
    # bare flag with no canonical home.
    "versender",
    "oneTimeCode",
    # Reported live on a reporter's account (issue #33). A flag on the
    # shipment, not a pickup state: DHL's own wording pairs a Packstation
    # used as the delivery address with the Kleinpaket product, and it names
    # no pickup point, so the structured `zustellung` checks stay the only
    # source for one.
    "psDirectAddressSmallParcel",
}

_INCOMING_SENDUNGSRICHTUNG_VALUES = {"ANKOMMEND", "EINGEHEND"}
# ABGEHEND is what a real account actually returns for an outgoing shipment.
# AUSGEHEND comes from two OSS sources and has never been seen on the wire, so
# it stays recognised rather than being swapped out.
_OUTGOING_SENDUNGSRICHTUNG_VALUES = {"AUSGEHEND", "ABGEHEND"}
_KNOWN_SENDUNGSRICHTUNG_VALUES = (
    _INCOMING_SENDUNGSRICHTUNG_VALUES | _OUTGOING_SENDUNGSRICHTUNG_VALUES
)
_sendungsrichtung_values_logged: set[str] = set()


def _warn_sendungsrichtung_value(value: str) -> None:
    """Log a `sendungsrichtung` value outside the confirmed vocabulary, once each."""
    if (
        value in _KNOWN_SENDUNGSRICHTUNG_VALUES
        or value in _sendungsrichtung_values_logged
    ):
        return
    _sendungsrichtung_values_logged.add(value)
    _LOGGER.warning(
        "DHL Germany reported a new sendungsinfo.sendungsrichtung value — "
        "help us enumerate it. Open an issue and paste this line: %s\n"
        "  value=%s",
        NEW_ISSUE_URL,
        value,
    )


def _direction(sendungsinfo: dict) -> str | None:
    """Return ``sendungsinfo.sendungsrichtung``, uppercased, or ``None``."""
    richtung = sendungsinfo.get("sendungsrichtung")
    return richtung.upper() if isinstance(richtung, str) else None


def _is_outgoing(sendungsinfo: dict) -> bool:
    """Whether this element's direction is one of the outgoing values."""
    return _direction(sendungsinfo) in _OUTGOING_SENDUNGSRICHTUNG_VALUES


def is_outgoing_element(raw: dict) -> bool:
    """Whether a ``sendungen[]`` element is outgoing.

    A missing/unrecognised direction defaults to incoming (mirrors
    ha-dhl-nl's ``_is_return`` default-safe behaviour) so an element with no
    known direction still surfaces somewhere rather than vanishing from every
    sensor.
    """
    sendungsinfo = raw.get("sendungsinfo")
    sendungsinfo = sendungsinfo if isinstance(sendungsinfo, dict) else {}
    return _is_outgoing(sendungsinfo)


def _sender_receiver(sendungsinfo: dict) -> tuple[str | None, str | None]:
    """Map ``sendungsinfo.sendungsname`` to `sender` or `receiver` by direction.

    Single confirmed field (issue #2): for an incoming shipment
    (``sendungsrichtung: ANKOMMEND``/``EINGEHEND`` — a second OSS source names
    the latter alongside the former) `sendungsname` names the sender; for an
    outgoing one (``ABGEHEND``/``AUSGEHEND``) it names the recipient instead. An
    unrecognised/missing direction leaves both `None` rather than guessing.
    """
    name = sendungsinfo.get("sendungsname")
    if not isinstance(name, str) or not name:
        return None, None
    # DHL returns the name HTML-escaped ("S &amp; T Logistik").
    name = html.unescape(name)
    richtung = _direction(sendungsinfo)
    if richtung is None:
        return None, None
    if richtung in _INCOMING_SENDUNGSRICHTUNG_VALUES:
        return name, None
    if richtung in _OUTGOING_SENDUNGSRICHTUNG_VALUES:
        return None, name
    _warn_sendungsrichtung_value(richtung)
    return None, None


def _sender_from_versender(details: dict) -> str | None:
    """Return `sendungsdetails.versender.name`, the shipper.

    A fallback only: `sendungsname` is the confirmed source, and that one
    names the recipient on an outgoing shipment, where this block still names
    the sender. The block itself was seen live carrying a single `name`
    (issue #26), which is what DHL calls the party a parcel was sent by.
    """
    versender = details.get("versender")
    if not isinstance(versender, dict):
        return None
    name = versender.get("name")
    if not isinstance(name, str) or not name:
        return None
    return html.unescape(name)


def _receiver_from_pan_empfaenger(details: dict) -> str | None:
    """Return `sendungsdetails.panEmpfaenger.name`, the addressee.

    A fallback only: `sendungsname` is the confirmed source, and this block
    is reconstruction-grade (issue #18 saw the key live but not its contents;
    a third OSS client reads its `name`/`ort` as the recipient's name and
    city). Unlike `.zustellung.empfaenger.name` it is not tied to who
    physically took the parcel, and it has been seen on an element that
    carried no `sendungsname` at all.
    """
    pan = details.get("panEmpfaenger")
    if not isinstance(pan, dict):
        return None
    name = pan.get("name")
    if not isinstance(name, str) or not name:
        return None
    return html.unescape(name)


_ANCHOR_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _is_ready_at_packstation(zustellung: dict) -> bool:
    """Both fields were seen together while a parcel waited in a Packstation.

    Both cleared once it was collected (issue #7). The type is matched on its
    prefix: a screen-less smart Packstation reports `PACKAGE_STATION_SMART`
    (issue #7, second reporter), so an exact match missed those parcels.
    """
    station_type = zustellung.get("packageStationType")
    return (
        isinstance(station_type, str)
        and station_type.startswith("PACKAGE_STATION")
        and zustellung.get("abholcodeAvailable") is True
    )


_READY_FOR_PICKUP_RE = re.compile(r"zur\s+abholung\s+bereit\b", re.IGNORECASE)


def _reads_ready_for_pickup(verlauf: dict, events: list) -> bool:
    """A Filiale/Paketshop arrival stays on rung 4; only the text says so.

    No structured field marks it, unlike a Packstation. Read from a
    third-party integration's fix, not yet seen on a parcel of ours.
    """
    newest = events[-1] if events and isinstance(events[-1], dict) else {}
    texts = (verlauf.get("status"), verlauf.get("kurzStatus"), newest.get("status"))
    return any(
        isinstance(text, str) and _READY_FOR_PICKUP_RE.search(_TAG_RE.sub("", text))
        for text in texts
    )


def _ready_event_texts(events: list) -> list[str]:
    """Status texts of the events that said a parcel was ready for pickup."""
    texts = []
    for event in events:
        status = event.get("status") if isinstance(event, dict) else None
        if isinstance(status, str) and _READY_FOR_PICKUP_RE.search(
            _TAG_RE.sub("", status)
        ):
            texts.append(status)
    return texts


def _pickup_point_from_events(events: list) -> str | None:
    """Return the location named by the link in the newest event that has one.

    The "ready for pickup" event embeds the location as an anchor
    (``<a …>Packstation 216, street, zip city</a>``); the anchor text is
    language-independent, unlike the sentence around it. Later events (the
    collection) carry no link, so the newest *linked* event is read and a
    collected parcel keeps its point.
    """
    for event in reversed(events):
        status = event.get("status") if isinstance(event, dict) else None
        if not isinstance(status, str):
            continue
        match = _ANCHOR_RE.search(status)
        if match is None:
            continue
        text = html.unescape(_TAG_RE.sub("", match.group(1)))
        name = " ".join(text.split(",")[0].split())
        return name or None
    return None


_unexpected_keys_logged: set[str] = set()
_delivered_conflict_logged: set[str] = set()
_raw_status_kurz_status_logged = False
_delivery_window_keys_logged: set[str] = set()
_live_tracking_window_logged = False
_timestamp_parse_failed_logged = False
_outgoing_status_unconfirmed_logged = False


def _warn_outgoing_status_unconfirmed_once() -> None:
    """One-shot warning for the first outgoing parcel seen.

    The fortschritt ladder has only ever been confirmed against incoming
    shipments — reporting it for an outgoing one would be a guess, so
    `status` stays UNKNOWN instead.
    """
    global _outgoing_status_unconfirmed_logged  # noqa: PLW0603 - one-shot warning flag, reset by tests
    if _outgoing_status_unconfirmed_logged:
        return
    _outgoing_status_unconfirmed_logged = True
    _LOGGER.warning(
        "DHL Germany reported an outgoing parcel — its "
        "fortschritt ladder was only ever confirmed against incoming "
        "shipments, so status is reported as 'unknown' rather than guessed. "
        "Open an issue with a real outgoing sendungsverlauf export: %s",
        NEW_ISSUE_URL,
    )


def _warn_unexpected_sendungsdetails_keys(details: dict) -> None:
    """One-shot-per-key: an unrecognised sendungsdetails key — likely the pickup-point field."""
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
    global _raw_status_kurz_status_logged  # noqa: PLW0603 - one-shot warning flag, reset by tests
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


# The `Von`/`Bis` pair arrived on a real account (issue #26), which settles
# the contested window shape in its favour; the singular keys stay
# reconstruction-grade, so only those still warn.
_CONFIRMED_DELIVERY_WINDOW_KEYS = frozenset(
    {"zustellzeitfensterVon", "zustellzeitfensterBis"}
)


def _warn_delivery_window_shape(keys_present: list[str]) -> None:
    """One-shot-per-key: a delivery-window key no real parcel has shown yet."""
    for key in keys_present:
        if (
            key in _CONFIRMED_DELIVERY_WINDOW_KEYS
            or key in _delivery_window_keys_logged
        ):
            continue
        _delivery_window_keys_logged.add(key)
        _LOGGER.warning(
            "DHL Germany response carried the unconfirmed delivery-window "
            "field %s. Open an issue and paste this line: %s\n  keys=%s",
            key,
            NEW_ISSUE_URL,
            keys_present,
        )


def _warn_live_tracking_window_once(timeframe: Any) -> None:
    """One-shot: the live-tracking timeframe filled a window the pair left open."""
    global _live_tracking_window_logged  # noqa: PLW0603 - one-shot warning flag, reset by tests
    if _live_tracking_window_logged:
        return
    _live_tracking_window_logged = True
    _LOGGER.warning(
        "DHL Germany's delivery window came from its live tracking for the "
        "first time. Open an issue and paste this line: %s\n  timeframe=%s",
        NEW_ISSUE_URL,
        timeframe,
    )


def _warn_timestamp_parse_failed_once(value: Any) -> None:
    global _timestamp_parse_failed_logged  # noqa: PLW0603 - one-shot warning flag, reset by tests
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

    No source ever supplied an example, so this accepts
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
    return parsed.astimezone(UTC).isoformat()


def _build_history_de(
    events: list[Any], *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build canonical ``history`` from ``sendungsverlauf.events[]``.

    Sorted defensively — the three reconstruction sources hold *incompatible*
    assumptions about wire order, so nothing here relies
    on it. ``status`` stays ``None`` on every entry: the free-text
    ``events[].status`` is an open, localised vocabulary no source
    enumerates, and it must never be keyed off for logic.
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


def _is_date_only(value: Any) -> bool:
    """Whether a DHL DE date/time value carries no time of day at all."""
    return isinstance(value, str) and "T" not in value and ":" not in value


def _delivery_window(zustellung: dict) -> tuple[str | None, str | None]:
    """Probe all four contested delivery-window keys.

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
        _warn_delivery_window_shape(present)

    if von or bis:
        start, end = von, bis
    elif fenster:
        start, end = fenster, None
    elif datum:
        start, end = datum, None
    else:
        return None, None
    planned_from, planned_to = planned_window(start, end, zone=_BERLIN)
    for value, result in ((start, planned_from), (end, planned_to)):
        if value and result is None:
            _parse_de_timestamp(value)
    return _in_utc(planned_from), _in_utc(planned_to, keep_end_of_day=True)


_TIME_OF_DAY_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def _live_tracking_window(
    live_tracking: Any, zustellung: dict
) -> tuple[str | None, str | None] | None:
    """The live-tracking timeframe, when the ``Von``/``Bis`` pair has no time of day.

    A timed pair wins. A bare time is placed on the pair's day, never on a
    guessed one.
    """
    if not isinstance(live_tracking, dict):
        return None
    timeframe = live_tracking.get("timeframe")
    if not isinstance(timeframe, dict) or not timeframe.get("from"):
        return None
    von = zustellung.get("zustellzeitfensterVon")
    bis = zustellung.get("zustellzeitfensterBis")
    if any(value and not _is_date_only(value) for value in (von, bis)):
        return None
    day = von if _is_date_only(von) else bis if _is_date_only(bis) else None
    ends: list[str | None] = []
    for value in (timeframe.get("from"), timeframe.get("to")):
        if not isinstance(value, str) or not value.strip():
            ends.append(None)
        elif _TIME_OF_DAY_RE.match(value.strip()) and day:
            ends.append(f"{str(day)[:10]}T{value.strip()}")
        elif "T" in value:
            ends.append(value.strip())
        else:
            _warn_live_tracking_window_once(timeframe)
            return None
    planned_from, planned_to = planned_window(ends[0], ends[1], zone=_BERLIN)
    _warn_live_tracking_window_once(timeframe)
    if planned_from is None:
        return None
    return _in_utc(planned_from), _in_utc(planned_to, keep_end_of_day=True)


def _in_utc(value: str | None, *, keep_end_of_day: bool = False) -> str | None:
    """Re-express a window timestamp in UTC, the spelling this source publishes.

    An end of day stays in Berlin time, so it still reads ``23:59:59``.
    """
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    local = parsed.astimezone(_BERLIN)
    if keep_end_of_day and local.time().replace(microsecond=0) == time(23, 59, 59):
        return local.isoformat()
    return parsed.astimezone(UTC).isoformat()


def _is_coordinate(key: str, value: Any) -> bool:
    return key.lower().endswith("coords") or (
        isinstance(value, dict) and {"lat", "lng"} <= value.keys()
    )


def _published_raw(raw: dict) -> dict:
    """The element as it may be published: never a position or a token.

    Live tracking holds the van's position and the destination's, which is
    the recipient's address; `services.verfuegen.zsmShipmentToken` is what
    DHL's "Sendung verfügen" accepts to change the delivery. Attributes and
    event data (stored by the recorder) carry `raw`, so both are left out.
    The input is never changed.
    """
    details = raw.get("sendungsdetails")
    if not isinstance(details, dict):
        return dict(raw)
    details = dict(details)
    live_tracking = details.get("liveTracking")
    if isinstance(live_tracking, dict):
        details["liveTracking"] = {
            key: value
            for key, value in live_tracking.items()
            if not _is_coordinate(key, value)
        }
    services = details.get("services")
    if isinstance(services, dict) and isinstance(services.get("verfuegen"), dict):
        details["services"] = {
            **services,
            "verfuegen": {
                key: value
                for key, value in services["verfuegen"].items()
                if key != "zsmShipmentToken"
            },
        }
    return {**raw, "sendungsdetails": details}


def normalize_parcel_de(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict for one ``sendungen`` element.

    Payload confirmed against a real account; every key is still guarded
    since a genuinely unrecognised field remains possible on other accounts.
    """
    barcode = raw.get("id")

    sendungsinfo = raw.get("sendungsinfo")
    sendungsinfo = sendungsinfo if isinstance(sendungsinfo, dict) else {}
    sender, receiver = _sender_receiver(sendungsinfo)
    is_outgoing = _is_outgoing(sendungsinfo)

    details = raw.get("sendungsdetails")
    details = details if isinstance(details, dict) else {}
    _warn_unexpected_sendungsdetails_keys(details)
    if sender is None:
        sender = _sender_from_versender(details)
    if receiver is None:
        receiver = _receiver_from_pan_empfaenger(details)

    verlauf = details.get("sendungsverlauf")
    verlauf = verlauf if isinstance(verlauf, dict) else {}

    fortschritt = verlauf.get("fortschritt")
    maximal_fortschritt = verlauf.get("maximalFortschritt")

    # Contested: raw_status prefers sendungsverlauf.status (2/3 sources);
    # kurzStatus is logged but not used as the primary text.
    raw_status = verlauf.get("status") or None
    kurz_status = verlauf.get("kurzStatus")
    if kurz_status:
        _warn_raw_status_kurz_status_once(bool(raw_status))
    if raw_status is None:
        raw_status = kurz_status

    if is_outgoing:
        _warn_outgoing_status_unconfirmed_once()
        status = ParcelStatus.UNKNOWN
    else:
        status = map_parcel_status_de(fortschritt, maximal_fortschritt)

    # Contested: two sources read istZugestellt, one derives from the
    # ladder. Read the flag when present, else derive; warn once per parcel
    # if they ever disagree. The flag decides the status, so delivered
    # follows the status below.
    delivered_flag = details.get("istZugestellt")
    try:
        derived_delivered = int(fortschritt) >= int(maximal_fortschritt or 5)  # type: ignore[arg-type]  # None raises TypeError, handled below
    except (TypeError, ValueError):
        derived_delivered = status is ParcelStatus.DELIVERED
    if isinstance(delivered_flag, bool):
        journey_over = delivered_flag
        if delivered_flag != derived_delivered:
            _warn_delivered_conflict(barcode)
    else:
        journey_over = derived_delivered
    if journey_over and status is not ParcelStatus.DELIVERED:
        status = ParcelStatus.DELIVERED
    elif not journey_over and status is ParcelStatus.DELIVERED:
        status = ParcelStatus.IN_TRANSIT

    events = verlauf.get("events")
    events = events if isinstance(events, list) else []
    history = _build_history_de(events) if include_history else None

    zustellung = details.get("zustellung")
    zustellung = zustellung if isinstance(zustellung, dict) else {}
    planned_from, planned_to = _live_tracking_window(
        details.get("liveTracking"), zustellung
    ) or _delivery_window(zustellung)

    at_pickup_point = _is_ready_at_packstation(zustellung) or _reads_ready_for_pickup(
        verlauf, events
    )
    if status is ParcelStatus.OUT_FOR_DELIVERY and at_pickup_point:
        status = ParcelStatus.AT_PICKUP_POINT

    # Confirmed live: shipment-level `retoure`/`ruecksendung` flags, distinct
    # from the per-event `events[].ruecksendung` — ORed since either can be
    # the one actually set.
    retoure = details.get("retoure")
    ruecksendung = details.get("ruecksendung")
    if (bool(retoure) or bool(ruecksendung)) and not is_outgoing:
        status = ParcelStatus.RETURNING

    # A return that DHL reports finished is back at the sender.
    delivered = status is ParcelStatus.DELIVERED or (
        status is ParcelStatus.RETURNING and journey_over
    )
    delivered_at = (
        _parse_de_timestamp(verlauf.get("datumAktuellerStatus")) if delivered else None
    )
    planned_from, planned_to = planned_for_status(status, planned_from, planned_to)
    pickup = is_pickup(
        at_pickup_point,
        status,
        (ParcelStatus.AT_PICKUP_POINT for _ in _ready_event_texts(events)),
    )
    pickup_point = _pickup_point_from_events(events) if pickup else None

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
        # confirms the semantics. It is still present,
        # redacted, inside `raw`.
        "sender": sender,
        "receiver": receiver,
        "contents": None,
        "status": status,
        "raw_status": raw_status,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": pickup,
        "pickup_point": pickup_point,
        "url": tracking_url,
        "weight": None,
        "dimensions": None,
        "history": history,
        "raw": _published_raw(raw),
    }


__all__ = [
    "async_get_by_number_envelope",
    "async_get_inbox_envelope",
    "find_element_by_id",
    "is_not_found",
    "is_outgoing_element",
    "map_parcel_status_de",
    "needs_enrichment",
    "normalize_parcel_de",
    "select_active_elements",
]
