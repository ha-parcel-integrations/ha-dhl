"""Tests for DHL Germany transport, selection, status map and normalize_parcel_de.

Fixtures come from ``tests/payloads.py``. Every still-contested field
(raw_status) is tested on both branches rather than just the preferred one.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.dhl.const import DHLApiError, DHLAuthError, ParcelStatus
from custom_components.dhl.countries import de as de_module
from custom_components.dhl.countries.de import (
    async_get_by_number_envelope,
    async_get_inbox_envelope,
    find_element_by_id,
    is_not_found,
    map_parcel_status_de,
    normalize_parcel_de,
    select_active_elements,
)
from custom_components.dhl.countries.de.session import DHLDeAuthError, DHLDeSession

from ..payloads import (
    ACTIVE_CODE,
    active_sample,
    archived_sample,
    delivered_sample,
    element,
    event,
    not_found_sample,
)


@pytest.fixture(autouse=True)
def _reset_one_shot_state():
    """Keep countries/de's one-shot WARNING dedup state isolated per test."""
    de_module._sendungsliste_values_logged.clear()
    de_module._sendungsrichtung_values_logged.clear()
    de_module._unmapped_fortschritt_logged.clear()
    de_module._maximal_fortschritt_logged = False
    de_module._unexpected_keys_logged.clear()
    de_module._delivered_conflict_logged.clear()
    de_module._raw_status_kurz_status_logged = False
    de_module._delivery_window_shape_logged = False
    de_module._timestamp_parse_failed_logged = False
    yield
    de_module._sendungsliste_values_logged.clear()
    de_module._sendungsrichtung_values_logged.clear()
    de_module._unmapped_fortschritt_logged.clear()
    de_module._maximal_fortschritt_logged = False
    de_module._unexpected_keys_logged.clear()
    de_module._delivered_conflict_logged.clear()
    de_module._raw_status_kurz_status_logged = False
    de_module._delivery_window_shape_logged = False
    de_module._timestamp_parse_failed_logged = False


# ---------------------------------------------------------------------------
# element selection
# ---------------------------------------------------------------------------


def test_select_active_elements_filters_archived():
    elements = [active_sample(), archived_sample()]
    survivors = select_active_elements(elements)
    assert [e["id"] for e in survivors] == [ACTIVE_CODE]


def test_select_active_elements_falls_back_when_all_archived():
    elements = [archived_sample("A"), archived_sample("B")]
    assert len(select_active_elements(elements)) == 2


def test_select_active_elements_ignores_non_dict_entries():
    assert select_active_elements([active_sample(), "junk", None]) == [
        active_sample()
    ]


def test_select_active_elements_warns_unrecognised_sendungsliste_value(caplog):
    unknown = element(ACTIVE_CODE, fortschritt=4, sendungsliste="WHATEVER")
    select_active_elements([unknown])
    assert "sendungsliste" in caplog.text.lower()


def test_select_active_elements_does_not_warn_on_known_sendungsliste_values(caplog):
    select_active_elements([active_sample(), archived_sample()])
    assert "sendungsliste" not in caplog.text.lower()


def test_find_element_by_id_matches():
    elements = [active_sample("A"), active_sample("B")]
    assert find_element_by_id(elements, "B")["id"] == "B"


def test_find_element_by_id_falls_back_to_first():
    elements = [active_sample("A"), active_sample("B")]
    assert find_element_by_id(elements, "NOPE")["id"] == "A"


def test_find_element_by_id_empty_list():
    assert find_element_by_id([], "A") is None


def test_is_not_found_explicit_marker():
    assert is_not_found(not_found_sample()) is True


def test_is_not_found_structural_backstop():
    empty = {"id": "X", "sendungsdetails": {"sendungsverlauf": {}}}
    assert is_not_found(empty) is True


def test_is_not_found_false_for_real_parcel():
    assert is_not_found(active_sample()) is False


def test_is_not_found_true_when_no_sendungsdetails():
    assert is_not_found({"id": "X"}) is True


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------


def _response(status: int, body: dict) -> AsyncMock:
    response = AsyncMock()
    response.status = status
    response.json = AsyncMock(return_value=body)
    return response


def _session_returning(status: int, body: dict) -> MagicMock:
    response = _response(status, body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


def _de_session(id_token: str = "id-token") -> MagicMock:
    de_session = MagicMock(spec=DHLDeSession)
    de_session.async_get_id_token = AsyncMock(return_value=id_token)
    de_session.async_handle_unauthorized = AsyncMock(return_value=id_token)
    return de_session


async def test_get_inbox_envelope_returns_body():
    session = _session_returning(200, {"sendungen": [], "rateLimited": False})
    envelope = await async_get_inbox_envelope(session, _de_session())
    assert envelope == {"sendungen": [], "rateLimited": False}


async def test_get_by_number_envelope_sends_piececode():
    session = _session_returning(200, {"sendungen": [active_sample()]})
    await async_get_by_number_envelope(session, _de_session(), ACTIVE_CODE)
    params = session.get.call_args.kwargs["params"]
    assert params["piececode"] == ACTIVE_CODE
    assert params["cid"] == "app"


async def test_transport_retries_once_on_401():
    de_session = _de_session()
    response_401 = _response(401, {})
    response_200 = _response(200, {"sendungen": []})
    ctx_401 = MagicMock()
    ctx_401.__aenter__ = AsyncMock(return_value=response_401)
    ctx_401.__aexit__ = AsyncMock(return_value=False)
    ctx_200 = MagicMock()
    ctx_200.__aenter__ = AsyncMock(return_value=response_200)
    ctx_200.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(side_effect=[ctx_401, ctx_200])

    envelope = await async_get_inbox_envelope(session, de_session)

    assert envelope == {"sendungen": []}
    de_session.async_handle_unauthorized.assert_awaited_once()


async def test_transport_raises_dhl_api_error_on_other_status():
    session = _session_returning(503, {})
    with pytest.raises(DHLApiError):
        await async_get_inbox_envelope(session, _de_session())


async def test_transport_raises_dhl_api_error_on_non_object_body():
    session = _session_returning(200, [])
    with pytest.raises(DHLApiError):
        await async_get_inbox_envelope(session, _de_session())


async def test_transport_converts_rejected_refresh_token_to_auth_error():
    de_session = MagicMock(spec=DHLDeSession)
    de_session.async_get_id_token = AsyncMock(
        side_effect=DHLDeAuthError("refresh token rejected")
    )
    with pytest.raises(DHLAuthError):
        await async_get_inbox_envelope(_session_returning(200, {}), de_session)


# ---------------------------------------------------------------------------
# status map — the fortschritt ladder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fortschritt,expected",
    [
        (0, ParcelStatus.REGISTERED),
        (1, ParcelStatus.REGISTERED),
        (2, ParcelStatus.IN_TRANSIT),  # settled: "Im Zustellzentrum"
        (3, ParcelStatus.IN_TRANSIT),
        (4, ParcelStatus.OUT_FOR_DELIVERY),
        (5, ParcelStatus.DELIVERED),
    ],
)
def test_status_ladder(fortschritt, expected):
    assert map_parcel_status_de(fortschritt, 5) == expected


def test_status_out_of_range_warns_and_returns_unknown(caplog):
    assert map_parcel_status_de(9, 5) == ParcelStatus.UNKNOWN
    assert "unrecognised" in caplog.text.lower()


def test_status_missing_fortschritt_is_unknown():
    assert map_parcel_status_de(None, 5) == ParcelStatus.UNKNOWN


def test_maximal_fortschritt_defaults_when_absent():
    assert map_parcel_status_de(5, None) == ParcelStatus.DELIVERED


def test_maximal_fortschritt_defaults_when_non_positive():
    assert map_parcel_status_de(0, 0) == ParcelStatus.REGISTERED


def test_maximal_fortschritt_unexpected_value_warns_once(caplog):
    map_parcel_status_de(6, 6)
    assert "maximalfortschritt=6" in caplog.text.lower()


def test_value_equal_to_maximal_is_always_delivered():
    assert map_parcel_status_de(6, 6) == ParcelStatus.DELIVERED


# ---------------------------------------------------------------------------
# normalize_parcel_de
# ---------------------------------------------------------------------------


def test_normalize_active_parcel():
    parcel = normalize_parcel_de(active_sample())
    assert parcel["carrier"] == "DHL"
    assert parcel["barcode"] == ACTIVE_CODE
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["raw_status"] == "Die Sendung befindet sich im Zustellfahrzeug."
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["planned_from"] == "2026-04-29T11:00:00+00:00"
    assert parcel["planned_to"] == "2026-04-29T13:00:00+00:00"
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["pickup_point"] is None
    assert parcel["sender"] is None
    assert parcel["receiver"] is None
    assert parcel["url"] == (
        "https://www.dhl.de/de/privatkunden/pakete-empfangen/verfolgen.html"
        f"?piececode={ACTIVE_CODE}"
    )
    assert parcel["raw"]["id"] == ACTIVE_CODE


def test_normalize_delivered_parcel_clears_eta():
    parcel = normalize_parcel_de(delivered_sample())
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-29T11:12:42+00:00"
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_normalize_incoming_maps_sendungsname_to_sender():
    raw = element(
        ACTIVE_CODE,
        fortschritt=4,
        sendungsname="Example Shop GmbH",
        sendungsrichtung="ANKOMMEND",
    )
    parcel = normalize_parcel_de(raw)
    assert parcel["sender"] == "Example Shop GmbH"
    assert parcel["receiver"] is None


def test_normalize_eingehend_also_maps_sendungsname_to_sender():
    raw = element(
        ACTIVE_CODE,
        fortschritt=4,
        sendungsname="Example Shop GmbH",
        sendungsrichtung="EINGEHEND",
    )
    parcel = normalize_parcel_de(raw)
    assert parcel["sender"] == "Example Shop GmbH"
    assert parcel["receiver"] is None


def test_normalize_outgoing_maps_sendungsname_to_receiver():
    raw = element(
        ACTIVE_CODE,
        fortschritt=4,
        sendungsname="Jane Doe",
        sendungsrichtung="AUSGEHEND",
    )
    parcel = normalize_parcel_de(raw)
    assert parcel["sender"] is None
    assert parcel["receiver"] == "Jane Doe"


def test_normalize_unrecognised_sendungsrichtung_leaves_both_none(caplog):
    raw = element(
        ACTIVE_CODE,
        fortschritt=4,
        sendungsname="Example Shop GmbH",
        sendungsrichtung="WHATEVER",
    )
    parcel = normalize_parcel_de(raw)
    assert parcel["sender"] is None
    assert parcel["receiver"] is None
    assert "sendungsrichtung" in caplog.text.lower()


def test_normalize_missing_barcode_url_is_bare():
    raw = active_sample()
    raw["id"] = None
    parcel = normalize_parcel_de(raw)
    assert parcel["url"] == (
        "https://www.dhl.de/de/privatkunden/pakete-empfangen/verfolgen.html"
    )


# --- the delivered flag: contested -------------------------------------


def test_delivered_flag_read_when_present_and_true():
    raw = element(ACTIVE_CODE, fortschritt=3, ist_zugestellt=True)
    assert normalize_parcel_de(raw)["delivered"] is True


def test_delivered_derived_when_flag_absent():
    raw = element(ACTIVE_CODE, fortschritt=5)
    assert "istZugestellt" not in raw["sendungsdetails"]
    assert normalize_parcel_de(raw)["delivered"] is True


def test_delivered_conflict_between_flag_and_derivation_warns_once(caplog):
    raw = element(ACTIVE_CODE, fortschritt=5, maximal_fortschritt=5, ist_zugestellt=False)
    parcel = normalize_parcel_de(raw)
    assert parcel["delivered"] is False  # explicit flag wins
    assert "disagrees" in caplog.text.lower()


# --- raw_status: contested ----------------------------------------------


def test_raw_status_prefers_status_over_kurz_status():
    raw = element(ACTIVE_CODE, fortschritt=3, status_text="Status Text", kurz_status="Kurz")
    assert normalize_parcel_de(raw)["raw_status"] == "Status Text"


def test_raw_status_falls_back_to_kurz_status():
    raw = element(ACTIVE_CODE, fortschritt=3, status_text=None, kurz_status="Kurz")
    assert normalize_parcel_de(raw)["raw_status"] == "Kurz"


def test_kurz_status_presence_warns_once(caplog):
    raw = element(ACTIVE_CODE, fortschritt=3, status_text="Status", kurz_status="Kurz")
    normalize_parcel_de(raw)
    assert "kurzstatus" in caplog.text.lower()


# --- delivery window: contested ------------------------------------------


def test_delivery_window_prefers_von_bis_pair():
    raw = element(
        ACTIVE_CODE,
        fortschritt=3,
        zustellung={
            "zustellzeitfensterVon": "2026-04-29T13:00:00+02:00",
            "zustellzeitfensterBis": "2026-04-29T15:00:00+02:00",
            "zustellzeitfenster": "13-15 Uhr",
        },
    )
    parcel = normalize_parcel_de(raw)
    assert parcel["planned_from"] == "2026-04-29T11:00:00+00:00"
    assert parcel["planned_to"] == "2026-04-29T13:00:00+00:00"


def test_delivery_window_falls_back_to_singular_fenster():
    raw = element(
        ACTIVE_CODE,
        fortschritt=3,
        zustellung={"zustellzeitfenster": "2026-04-29T13:00:00+02:00"},
    )
    parcel = normalize_parcel_de(raw)
    assert parcel["planned_from"] == "2026-04-29T11:00:00+00:00"
    assert parcel["planned_to"] is None


def test_delivery_window_falls_back_to_zustelldatum():
    raw = element(
        ACTIVE_CODE,
        fortschritt=3,
        zustellung={"zustelldatum": "2026-04-29T00:00:00+02:00"},
    )
    parcel = normalize_parcel_de(raw)
    assert parcel["planned_from"] == "2026-04-28T22:00:00+00:00"


def test_delivery_window_absent_is_none():
    raw = element(ACTIVE_CODE, fortschritt=3, zustellung={})
    parcel = normalize_parcel_de(raw)
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_delivery_window_shape_warns_once(caplog):
    raw = element(
        ACTIVE_CODE,
        fortschritt=3,
        zustellung={"zustellzeitfenster": "2026-04-29T13:00:00+02:00"},
    )
    normalize_parcel_de(raw)
    assert "delivery-window" in caplog.text.lower()


# --- returning: candidate mechanism --------------------------------------


def test_retoure_true_maps_to_returning():
    raw = element(ACTIVE_CODE, fortschritt=3, retoure=True)
    assert normalize_parcel_de(raw)["status"] == ParcelStatus.RETURNING


def test_ruecksendung_true_maps_to_returning():
    raw = element(ACTIVE_CODE, fortschritt=3, ruecksendung=True)
    assert normalize_parcel_de(raw)["status"] == ParcelStatus.RETURNING


# --- history: sorted defensively, status always None --------------------


def test_history_sorted_oldest_to_newest_regardless_of_wire_order():
    raw = element(
        ACTIVE_CODE,
        fortschritt=3,
        events=[
            event("newest", "2026-04-29T13:00:00+02:00"),
            event("oldest", "2026-04-27T10:00:00+02:00"),
            event("middle", "2026-04-28T10:00:00+02:00"),
        ],
    )
    history = normalize_parcel_de(raw, include_history=True)["history"]
    assert [entry["raw_status"] for entry in history] == ["oldest", "middle", "newest"]


def test_history_status_is_always_none():
    raw = element(
        ACTIVE_CODE, fortschritt=3, events=[event("x", "2026-04-29T13:00:00+02:00")]
    )
    history = normalize_parcel_de(raw, include_history=True)["history"]
    assert history[0]["status"] is None


def test_history_capped_at_20():
    events = [event(f"e{i}", f"2026-04-{i + 1:02d}T00:00:00+02:00") for i in range(25)]
    raw = element(ACTIVE_CODE, fortschritt=3, events=events)
    history = normalize_parcel_de(raw, include_history=True)["history"]
    assert len(history) == 20


def test_history_skips_unparseable_timestamps(caplog):
    raw = element(
        ACTIVE_CODE,
        fortschritt=3,
        events=[{"datum": "not-a-date", "status": "x"}],
    )
    history = normalize_parcel_de(raw, include_history=True)["history"]
    assert history == []
    assert "could not parse" in caplog.text.lower()


def test_history_none_when_option_off():
    raw = active_sample()
    assert normalize_parcel_de(raw, include_history=False)["history"] is None


def test_naive_event_timestamp_assumed_berlin():
    raw = element(
        ACTIVE_CODE,
        fortschritt=3,
        events=[event("x", "2026-06-15T12:00:00")],  # naive, CEST in June
    )
    history = normalize_parcel_de(raw, include_history=True)["history"]
    assert history[0]["timestamp"] == "2026-06-15T10:00:00+00:00"


# --- unexpected keys / payload shape -------------------------------------


def test_unexpected_sendungsdetails_key_warns_once(caplog):
    raw = active_sample()
    raw["sendungsdetails"]["neverSeenBefore"] = "x"
    normalize_parcel_de(raw)
    assert "neverseenbefore" in caplog.text.lower()


# ---------------------------------------------------------------------------
# every one-shot WARNING only fires once, even across many calls
# ---------------------------------------------------------------------------


async def test_transport_generic_session_error_becomes_api_error():
    from custom_components.dhl.countries.de.session import DHLDeSessionError

    de_session = MagicMock(spec=DHLDeSession)
    de_session.async_get_id_token = AsyncMock(side_effect=DHLDeSessionError("outage"))
    with pytest.raises(DHLApiError):
        await async_get_inbox_envelope(_session_returning(200, {}), de_session)


def test_sendungsliste_warning_fires_only_once(caplog):
    unknown_a = element("A", fortschritt=4, sendungsliste="WHATEVER")
    unknown_b = element("B", fortschritt=4, sendungsliste="WHATEVER")
    select_active_elements([unknown_a, unknown_b])
    assert caplog.text.lower().count("sendungsliste") == 1


def test_unexpected_sendungsdetails_key_fires_only_once(caplog):
    raw1 = active_sample("A")
    raw1["sendungsdetails"]["neverSeenBefore"] = "x"
    raw2 = active_sample("B")
    raw2["sendungsdetails"]["neverSeenBefore"] = "y"
    normalize_parcel_de(raw1)
    normalize_parcel_de(raw2)
    assert caplog.text.lower().count("unrecognised field") == 1


def test_delivered_conflict_warns_once_per_barcode(caplog):
    raw = element(ACTIVE_CODE, fortschritt=5, maximal_fortschritt=5, ist_zugestellt=False)
    normalize_parcel_de(raw)
    normalize_parcel_de(raw)
    assert caplog.text.lower().count("disagrees") == 1


def test_kurz_status_warning_fires_only_once(caplog):
    raw = element(ACTIVE_CODE, fortschritt=3, status_text="Status", kurz_status="Kurz")
    normalize_parcel_de(raw)
    normalize_parcel_de(raw)
    assert caplog.text.lower().count("carried kurzstatus for the first time") == 1


def test_delivery_window_warning_fires_only_once(caplog):
    raw = element(
        ACTIVE_CODE,
        fortschritt=3,
        zustellung={"zustellzeitfenster": "2026-04-29T13:00:00+02:00"},
    )
    normalize_parcel_de(raw)
    normalize_parcel_de(raw)
    assert caplog.text.lower().count("delivery-window") == 1


def test_maximal_fortschritt_warning_fires_only_once(caplog):
    map_parcel_status_de(6, 6)
    map_parcel_status_de(6, 6)
    assert caplog.text.lower().count("maximalfortschritt=6") == 1


def test_derived_delivered_defaults_to_status_when_fortschritt_unparseable():
    raw = element(ACTIVE_CODE, fortschritt="not-a-number", maximal_fortschritt=5)
    parcel = normalize_parcel_de(raw)
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
