"""Tests for api/parcels.py: the statusCode baseline and field mapping."""
import logging

import pytest

from custom_components.dhl.api import parcels as api_parcels
from custom_components.dhl.api.parcels import normalize_parcel_unified, pick_shipment
from custom_components.dhl.const import (
    CAPABILITIES_BY_VARIANT,
    HISTORY_MAX_EVENTS,
    ParcelStatus,
)

from .payloads import body, event, shipment

CANONICAL_KEYS = {
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
}


def _normalize(raw: dict, **kwargs) -> dict:
    return normalize_parcel_unified(raw, code=raw.get("id") or "CODE", **kwargs)


def test_publishes_exactly_the_canonical_keys():
    assert set(_normalize(shipment())) == CANONICAL_KEYS


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        ("pre-transit", ParcelStatus.REGISTERED),
        ("transit", ParcelStatus.IN_TRANSIT),
        ("delivered", ParcelStatus.DELIVERED),
        ("failure", ParcelStatus.PROBLEM),
        ("unknown", ParcelStatus.UNKNOWN),
    ],
)
def test_status_code_baseline(status_code, expected):
    parcel = _normalize(shipment(status_code=status_code))
    assert parcel["status"] == expected
    assert parcel["delivered"] is (status_code == "delivered")


def test_unrecognised_status_code_warns_once_without_the_barcode(caplog):
    with caplog.at_level(logging.WARNING):
        first = _normalize(shipment("SECRET123", status_code="lost-in-space"))
        _normalize(shipment("SECRET123", status_code="lost-in-space"))

    assert first["status"] == ParcelStatus.UNKNOWN
    warnings = [r for r in caplog.records if "lost-in-space" in r.getMessage()]
    assert len(warnings) == 1
    assert "SECRET123" not in warnings[0].getMessage()


def test_fine_status_text_does_not_refine_until_confirmed():
    parcel = _normalize(shipment(status_code="transit", status="OUT FOR DELIVERY"))
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["raw_status"] == "OUT FOR DELIVERY"


def test_a_confirmed_refinement_wins_over_the_baseline(monkeypatch):
    monkeypatch.setitem(
        api_parcels._REFINEMENTS,
        ("parcel-de", "OUT FOR DELIVERY"),
        ParcelStatus.OUT_FOR_DELIVERY,
    )
    parcel = _normalize(shipment(status_code="transit", status="OUT FOR DELIVERY"))
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY


def test_delivered_at_is_the_newest_delivered_event():
    events = [
        event("2026-09-28T12:00:00+02:00", "delivered", "DELIVERED"),
        event("2026-09-28T08:00:00+02:00", "transit", "OUT FOR DELIVERY"),
    ]
    parcel = _normalize(shipment(status_code="delivered", status="DELIVERED", events=events))
    assert parcel["delivered_at"] == "2026-09-28T12:00:00+02:00"


def test_delivered_at_falls_back_to_the_status_timestamp():
    parcel = _normalize(shipment(status_code="delivered", status="DELIVERED", events=[]))
    assert parcel["delivered_at"] == "2026-09-28T10:00:00"


def test_delivery_frame_is_passed_through_with_its_own_offset():
    parcel = _normalize(
        shipment(
            estimatedDeliveryTimeFrame={
                "estimatedFrom": "2026-09-29T10:00:00+02:00",
                "estimatedThrough": "2026-09-29T14:00:00+02:00",
            },
            estimatedTimeOfDelivery="2026-09-29T12:00:00+02:00",
        )
    )
    assert parcel["planned_from"] == "2026-09-29T10:00:00+02:00"
    assert parcel["planned_to"] == "2026-09-29T14:00:00+02:00"


def test_single_eta_is_a_point():
    parcel = _normalize(shipment(estimatedTimeOfDelivery="2026-09-29T12:00:00"))
    assert parcel["planned_from"] == parcel["planned_to"] == "2026-09-29T12:00:00"


def test_empty_frame_falls_back_to_the_eta():
    parcel = _normalize(
        shipment(
            estimatedDeliveryTimeFrame={},
            estimatedTimeOfDelivery="2026-09-29T12:00:00",
        )
    )
    assert parcel["planned_from"] == "2026-09-29T12:00:00"


def test_no_window_once_delivered():
    parcel = _normalize(
        shipment(status_code="delivered", estimatedTimeOfDelivery="2026-09-29T12:00:00")
    )
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (2.0, "kg", 2.0),
        (2.0, "KG", 2.0),
        (500, "g", 0.5),
        (0.831, "LB", 0.377),
        (16, "oz", 0.454),
    ],
)
def test_weight_is_converted_to_kg(value, unit, expected):
    raw = shipment()
    raw["details"]["weight"] = {"value": value, "unitText": unit}
    assert _normalize(raw)["weight"] == expected


def test_weight_in_an_unknown_unit_is_left_empty_and_warns_once(caplog):
    raw = shipment()
    raw["details"]["weight"] = {"value": 3, "unitText": "stone"}
    with caplog.at_level(logging.WARNING):
        assert _normalize(raw)["weight"] is None
        _normalize(raw)
    assert len([r for r in caplog.records if "stone" in r.getMessage()]) == 1


@pytest.mark.parametrize("weight", [None, "2 kg", {"value": "2", "unitText": "kg"}, {"value": True, "unitText": "kg"}])
def test_malformed_weight_is_none(weight):
    raw = shipment()
    raw["details"]["weight"] = weight
    assert _normalize(raw)["weight"] is None


def test_history_is_newest_first_and_capped():
    events = [
        event(f"2026-09-{day:02d}T10:00:00+02:00", "transit", f"STEP {day}")
        for day in range(1, 26)
    ]
    events.append({"timestamp": "garbage", "statusCode": "transit", "status": "UNDATED"})
    events.append("not an event")
    parcel = _normalize(shipment(events=list(reversed(events[:25])) + events[25:]), include_history=True)

    history = parcel["history"]
    assert len(history) == HISTORY_MAX_EVENTS
    assert history[0] == {
        "timestamp": "2026-09-25T10:00:00+02:00",
        "status": ParcelStatus.IN_TRANSIT,
        "raw_status": "STEP 25",
    }
    assert [h["raw_status"] for h in history] == [f"STEP {d}" for d in range(25, 5, -1)]


def test_history_off_by_default():
    assert _normalize(shipment())["history"] is None


def test_raw_is_the_whole_shipment_untrimmed():
    raw = shipment()
    assert _normalize(raw)["raw"] is raw


def test_placeholder_for_an_unfetched_code():
    parcel = normalize_parcel_unified({}, code="NOTYET")
    assert parcel["barcode"] == "NOTYET"
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["weight"] is None


def test_fields_the_api_does_not_confirm_stay_none():
    parcel = _normalize(shipment())
    for field in ("sender", "receiver", "pickup_point", "dimensions", "url"):
        assert parcel[field] is None
    assert parcel["pickup"] is False


def test_api_capabilities_match_what_the_normalizer_returns():
    capabilities = CAPABILITIES_BY_VARIANT["API Tracking"]
    parcel = _normalize(shipment(estimatedTimeOfDelivery="2026-09-29T12:00:00"), include_history=True)
    assert ("weight" in capabilities) == (parcel["weight"] is not None)
    assert ("delivery_window" in capabilities) == (parcel["planned_from"] is not None)
    assert ("history" in capabilities) == (parcel["history"] is not None)
    assert "dimensions" not in capabilities
    assert "pickup_point" not in capabilities


def test_pick_shipment_matches_the_code():
    picked = pick_shipment(body(shipment("OTHER"), shipment("abc123")), "ABC123")
    assert picked["id"] == "abc123"


def test_pick_shipment_falls_back_to_the_first_and_warns_once(caplog):
    with caplog.at_level(logging.WARNING):
        picked = pick_shipment(body(shipment("ONE"), shipment("TWO")), "THREE")
        pick_shipment(body(shipment("ONE"), shipment("TWO")), "THREE")
    assert picked["id"] == "ONE"
    assert len([r for r in caplog.records if "shipments for one" in r.getMessage()]) == 1


def test_pick_shipment_empty():
    assert pick_shipment({}, "X") is None
    assert pick_shipment({"shipments": ["junk"]}, "X") is None
