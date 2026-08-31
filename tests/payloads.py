"""Synthetic DHL Germany ``sendungen`` element samples.

Built from BUILD_PLAN.md §5's reconstructed field list — **no populated
element has ever been observed on the wire** by anyone in this suite
(`payload: reconstructed`). Kept in one module, mirroring the rest of the
suite's convention, so the eventual tester capture (§7c) only needs to
change values here rather than scattered test bodies.
"""
from __future__ import annotations

ACTIVE_CODE = "00340434161094681228"
DELIVERED_CODE = "00340434161094681111"


def event(status_text: str, timestamp: str) -> dict:
    """One entry of ``sendungsverlauf.events[]``."""
    return {"datum": timestamp, "status": status_text}


def element(
    code: str,
    *,
    fortschritt: int,
    maximal_fortschritt: int | None = 5,
    status_text: str | None = "Die Sendung wurde zugestellt.",
    kurz_status: str | None = None,
    ist_zugestellt: bool | None = None,
    datum_aktueller_status: str | None = "2026-04-29T13:12:42+02:00",
    events: list[dict] | None = None,
    zustellung: dict | None = None,
    sendungsliste: str | None = None,
    retoure: bool | None = None,
    ruecksendung: bool | None = None,
) -> dict:
    """Build one ``sendungen[i]`` element from the §5 field inventory."""
    sendungsverlauf: dict = {
        "fortschritt": fortschritt,
        "maximalFortschritt": maximal_fortschritt,
        "datumAktuellerStatus": datum_aktueller_status,
        "events": events if events is not None else [],
    }
    if status_text is not None:
        sendungsverlauf["status"] = status_text
    if kurz_status is not None:
        sendungsverlauf["kurzStatus"] = kurz_status

    sendungsdetails: dict = {"sendungsverlauf": sendungsverlauf}
    if ist_zugestellt is not None:
        sendungsdetails["istZugestellt"] = ist_zugestellt
    if zustellung is not None:
        sendungsdetails["zustellung"] = zustellung
    if retoure is not None:
        sendungsdetails["retoure"] = retoure
    if ruecksendung is not None:
        sendungsdetails["ruecksendung"] = ruecksendung

    built: dict = {"id": code, "sendungsdetails": sendungsdetails}
    if sendungsliste is not None:
        built["sendungsinfo"] = {"sendungsliste": sendungsliste}
    return built


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A representative delivered parcel."""
    return element(
        code,
        fortschritt=5,
        status_text="Die Sendung wurde zugestellt.",
        ist_zugestellt=True,
        events=[
            event("Die Sendung wurde eingeliefert.", "2026-04-27T23:03:58+02:00"),
            event("Die Sendung befindet sich im Zustellfahrzeug.", "2026-04-29T06:00:00+02:00"),
            event("Die Sendung wurde ausgeliefert.", "2026-04-29T08:46:00+02:00"),
            event("Die Sendung wurde zugestellt.", "2026-04-29T13:12:42+02:00"),
        ],
    )


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """An out-for-delivery parcel with a ``Von``/``Bis`` delivery window."""
    return element(
        code,
        fortschritt=4,
        status_text="Die Sendung befindet sich im Zustellfahrzeug.",
        ist_zugestellt=False,
        events=[
            event("Die Sendung wurde eingeliefert.", "2026-04-27T23:03:58+02:00"),
            event("Die Sendung befindet sich im Zustellfahrzeug.", "2026-04-29T06:00:00+02:00"),
        ],
        zustellung={
            "zustellzeitfensterVon": "2026-04-29T13:00:00+02:00",
            "zustellzeitfensterBis": "2026-04-29T15:00:00+02:00",
        },
    )


def in_transit_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel in transit (fortschritt=3, uncontested rung)."""
    return element(
        code,
        fortschritt=3,
        status_text="Die Sendung befindet sich im Verteilzentrum.",
        events=[event("Die Sendung wurde eingeliefert.", "2026-04-27T23:03:58+02:00")],
    )


def rung_two_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel at the contested rung 2."""
    return element(code, fortschritt=2, status_text="In Vorbereitung.")


def archived_sample(code: str = "ARCHIVED0001") -> dict:
    """An archived element — must be filtered out of the inbox by default."""
    return element(
        code, fortschritt=5, ist_zugestellt=True, sendungsliste="ARCHIVIERT"
    )


def not_found_sample(code: str = "UNKNOWN00001") -> dict:
    """A populated element that is not a real parcel (§5a)."""
    return {"id": code, "sendungNichtGefunden": {"keineDatenVerfuegbar": True}}


def stub_sample(code: str = ACTIVE_CODE) -> dict:
    """A bare inbox element the account listing hasn't detailed yet.

    No `sendungsverlauf` at all and no not-found marker — needs a by-number
    fetch to fill in.
    """
    return {
        "id": code,
        "sendungsinfo": {"sendungsliste": "AKTUELL"},
        "sendungsdetails": {"istZugestellt": False},
    }
