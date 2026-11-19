"""The parcel contract's checks, run over every source's normalised samples."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.dhl.account.countries.de import normalize_parcel_de
from custom_components.dhl.account.countries.pl import normalize_parcel_pl
from custom_components.dhl.api.parcels import normalize_parcel_unified
from custom_components.dhl.const import ParcelStatus
from custom_components.dhl.delivery_window import (
    is_pickup,
    planned_for_status,
    planned_window,
)
from custom_components.dhl.tracking.express import normalize_parcel_express
from custom_components.dhl.tracking.gateway import normalize_parcel_gateway
from custom_components.dhl.tracking.hamta import normalize_parcel_hamta
from custom_components.dhl.tracking.iberia import normalize_parcel_iberia
from custom_components.dhl.tracking.mojdhl import normalize_parcel_mojdhl

from .api.payloads import shipment
from .payloads import element
from .tracking.payloads import express_in_transit, gateway_element

CANONICAL_KEYS = [
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
]

_AMS = ZoneInfo("Europe/Amsterdam")
_PACKSTATION = {
    "packageStationType": "PACKAGE_STATION",
    "abholcodeAvailable": True,
}
_PACKSTATION_EVENT = (
    "Die Sendung liegt in der <a href='https://example.com/p'>Packstation 216, "
    "Hauptstr. 1, 12345 Berlin</a> zur Abholung bereit."
)


def _de(**kwargs):
    kwargs.setdefault("fortschritt", 3)
    return lambda: normalize_parcel_de(element("1", **kwargs))


def _mojdhl(**raw):
    base = {
        "shipmentNumber": "1",
        "status": "DELIVERED",
        "planOfDeliveryFromUtc": "2099-05-22T11:00:00Z",
        "planOfDeliveryToUtc": "2099-05-22T13:00:00Z",
    }
    return lambda: normalize_parcel_mojdhl({**base, **raw})


SAMPLES = {
    "de_active": _de(
        zustellung={
            "zustellzeitfensterVon": "2099-05-22T13:00:00+02:00",
            "zustellzeitfensterBis": "2099-05-22T15:00:00+02:00",
        }
    ),
    "de_moment": _de(zustellung={"zustelldatum": "2099-05-22T13:00:00+02:00"}),
    "de_date": _de(
        zustellung={
            "zustellzeitfensterVon": "2099-05-22",
            "zustellzeitfensterBis": "2099-05-23",
        }
    ),
    "de_at_packstation": _de(
        fortschritt=4,
        ist_zugestellt=False,
        zustellung=_PACKSTATION,
        events=[{"datum": "2099-05-22T10:00:00+02:00", "status": _PACKSTATION_EVENT}],
    ),
    # Issue #7's collected state: the station fields are gone, the pickup code
    # is no longer available, and a newer event says it was collected.
    "de_collected": _de(
        fortschritt=5,
        ist_zugestellt=True,
        zustellung={"abholcodeAvailable": False},
        events=[
            {"datum": "2099-05-22T10:00:00+02:00", "status": _PACKSTATION_EVENT},
            {
                "datum": "2099-05-23T09:00:00+02:00",
                "status": "Die Sendung wurde abgeholt.",
            },
        ],
    ),
    "de_delivered": _de(fortschritt=5, ist_zugestellt=True),
    "de_flag_without_ladder": _de(fortschritt=3, ist_zugestellt=True),
    "de_returning": _de(retoure=True),
    "de_returned": _de(fortschritt=5, ist_zugestellt=True, retoure=True),
    "de_outgoing_returned": _de(
        fortschritt=5,
        ist_zugestellt=True,
        retoure=True,
        sendungsname="Jane Doe",
        sendungsrichtung="AUSGEHEND",
    ),
    "gateway_active": lambda: normalize_parcel_gateway(gateway_element()),
    "gateway_delivered": lambda: normalize_parcel_gateway(
        gateway_element(
            category="DELIVERED",
            status="DELIVERED",
            delivered_at="2099-02-13T13:00:00+01:00",
        )
    ),
    "gateway_flag_only": lambda: normalize_parcel_gateway(
        gateway_element(delivered_at="2099-02-13T13:00:00+01:00")
    ),
    "express": lambda: normalize_parcel_express(express_in_transit()),
    "unified": lambda: normalize_parcel_unified(
        shipment(estimatedTimeOfDelivery="2099-09-29T12:00:00"), code="1"
    ),
    "unified_delivered": lambda: normalize_parcel_unified(
        shipment(status_code="delivered", status="DELIVERED"), code="1"
    ),
    "mojdhl_active": _mojdhl(status="IN_TRANSIT"),
    "mojdhl_delivered": _mojdhl(),
    "pl": lambda: normalize_parcel_pl({"shipmentNumber": "1", "status": "DELIVERED"}),
    "hamta": lambda: normalize_parcel_hamta({"trackingNumber": "1", "events": []}),
    "iberia": lambda: normalize_parcel_iberia(
        {"ExpeditionNumber": "1", "Tracking": []}
    ),
}


@pytest.fixture(params=SAMPLES)
def parcel(request, amsterdam) -> dict:
    return SAMPLES[request.param]()


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    if parcel["status"] is ParcelStatus.DELIVERED:
        assert parcel["delivered"] is True
    elif parcel["status"] is not ParcelStatus.RETURNING:
        assert parcel["delivered"] is False


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert datetime.fromisoformat(parcel["planned_to"]) > datetime.fromisoformat(
            parcel["planned_from"]
        )


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_a_collected_packstation_parcel_stays_pickup(amsterdam):
    parcel = SAMPLES["de_collected"]()
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Packstation 216"


def test_a_packstation_names_only_the_point(amsterdam):
    assert SAMPLES["de_at_packstation"]()["pickup_point"] == "Packstation 216"


def test_the_delivered_flag_decides_the_status(amsterdam):
    parcel = SAMPLES["de_flag_without_ladder"]()
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True


def test_a_returned_parcel_the_carrier_calls_finished_is_delivered(amsterdam):
    parcel = SAMPLES["de_returned"]()
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is True


def test_a_return_the_user_sends_is_delivered_on_arrival(amsterdam):
    parcel = SAMPLES["de_outgoing_returned"]()
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True


def test_a_gateway_return_stamped_delivered_is_finished_but_stays_returning(
    amsterdam,
):
    parcel = normalize_parcel_gateway(
        gateway_element(
            category="DELIVERED",
            status="DELIVERED_AT_SHIPPER",
            delivered_at="2099-02-13T13:00:00+01:00",
        )
    )
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2099-02-13T13:00:00+01:00"


def test_a_gateway_return_on_its_way_is_not_finished(amsterdam):
    parcel = normalize_parcel_gateway(
        gateway_element(category="UNDERWAY", status="ON_ROUTE_TO_SHIPPER")
    )
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False


def test_a_collected_mojdhl_locker_parcel_stays_pickup(amsterdam):
    parcel = normalize_parcel_pl(
        {
            "shipmentNumber": "1",
            "status": "TT_OP",
            "packageType": "Locker",
            "menuTimelineLabel": {"status": "RetrievedFromLocker"},
        }
    )
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["pickup"] is True


def test_a_polish_home_delivery_is_not_pickup(amsterdam):
    parcel = normalize_parcel_pl(
        {"shipmentNumber": "1", "status": "TT_DOR", "packageType": "Courier"}
    )
    assert parcel["pickup"] is False


def test_a_returning_parcel_is_not_delivered(amsterdam):
    parcel = SAMPLES["de_returning"]()
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False


def test_a_german_date_pair_covers_whole_days(amsterdam):
    parcel = SAMPLES["de_date"]()
    assert parcel["planned_from"] == "2099-05-21T22:00:00+00:00"
    assert parcel["planned_to"] == "2099-05-23T23:59:59+02:00"


def test_a_german_moment_ends_at_the_end_of_its_day(amsterdam):
    parcel = SAMPLES["de_moment"]()
    assert parcel["planned_to"] == "2099-05-22T23:59:59+02:00"


def test_a_gateway_moment_ends_at_the_end_of_the_local_day(amsterdam):
    parcel = normalize_parcel_gateway(gateway_element(moment="2099-02-13T13:00:00Z"))
    assert parcel["planned_to"] == "2099-02-13T23:59:59+01:00"


def test_the_gateway_delivery_time_comes_from_the_delivered_event(amsterdam):
    parcel = normalize_parcel_gateway(
        gateway_element(category="DELIVERED", status="DELIVERED")
    )
    assert parcel["delivered_at"] == "2026-02-13T09:28:29Z"


def test_mojdhl_window_stays_a_window(amsterdam):
    parcel = SAMPLES["mojdhl_active"]()
    assert parcel["planned_from"] == "2099-05-22T11:00:00+00:00"
    assert parcel["planned_to"] == "2099-05-22T13:00:00+00:00"


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (
            "2026-04-29T13:00:00+02:00",
            "2026-04-29T15:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (
            "2026-04-29T13:00:00+02:00",
            None,
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29T13:00:00+02:00",
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29T13:00:00",
            None,
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29",
            None,
            ("2026-04-29T00:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            date(2026, 4, 29),
            date(2026, 4, 29),
            ("2026-04-29T00:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29",
            "2026-05-01",
            ("2026-04-29T00:00:00+02:00", "2026-05-01T23:59:59+02:00"),
        ),
        (
            "2026-04-29T15:00:00+02:00",
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (
            None,
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            datetime(2026, 4, 29, 13, 0),
            datetime(2026, 4, 29, 15, 0),
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (None, None, (None, None)),
        ("garbage", None, (None, None)),
        (12, 13, (None, None)),
    ],
)
def test_planned_window_shapes(start, end, expected):
    assert planned_window(start, end, zone=_AMS) == expected


@pytest.mark.parametrize(
    "status",
    [ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED],
)
def test_planned_for_status_empties_the_window(status):
    assert planned_for_status(status, "a", "b") == (None, None)


def test_planned_for_status_keeps_the_window_otherwise():
    for status in (
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.PROBLEM,
        ParcelStatus.UNKNOWN,
    ):
        assert planned_for_status(status, "a", "b") == ("a", "b")


def test_is_pickup_sources():
    assert is_pickup(True, ParcelStatus.IN_TRANSIT) is True
    assert is_pickup(None, ParcelStatus.AT_PICKUP_POINT) is True
    assert is_pickup(None, ParcelStatus.DELIVERED, [None, ParcelStatus.AT_PICKUP_POINT])
    assert is_pickup(False, ParcelStatus.IN_TRANSIT, [ParcelStatus.IN_TRANSIT]) is False
    assert is_pickup(None, ParcelStatus.DELIVERED) is False
