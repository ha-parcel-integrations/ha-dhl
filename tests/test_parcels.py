"""Tests for the top-level parcel helpers: the country dispatcher, sort, filter.

The carrier-specific mapping and status map themselves are tested in
``tests/countries/test_de.py`` — this module only covers what stays generic
across a future second country: ``parse_iso``, ``sort_parcels_by_ts``,
``apply_delivered_filter``, and that ``normalize_parcel`` dispatches to the
right country and publishes exactly the canonical key set.
"""
from datetime import datetime, timedelta, timezone

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dhl.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    ParcelStatus,
)
from custom_components.dhl.parcels import (
    apply_delivered_filter,
    is_outgoing,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
)

from .payloads import active_sample, delivered_sample, element

# ---------------------------------------------------------------------------
# timestamp helper
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z").tzinfo is not None
    # A naive value is assumed UTC so mixed lists still sort.
    assert parse_iso("2026-04-29T13:12:42").tzinfo == timezone.utc
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


# ---------------------------------------------------------------------------
# normalize_parcel — dispatch + the canonical contract
# ---------------------------------------------------------------------------

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


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample(), country="DE")) == CANONICAL_KEYS


def test_normalize_dispatches_to_de():
    parcel = normalize_parcel(delivered_sample(), country="DE")
    assert parcel["carrier"] == "DHL"
    assert parcel["status"] == ParcelStatus.DELIVERED


def test_is_outgoing_dispatches_to_de():
    outgoing = element(
        "X", fortschritt=3, sendungsname="Jane Doe", sendungsrichtung="AUSGEHEND"
    )
    assert is_outgoing(outgoing, country="DE") is True
    assert is_outgoing(active_sample(), country="DE") is False


def test_sender_and_receiver_names_are_html_unescaped():
    incoming = element(
        "X", fortschritt=3, sendungsname="S &amp; T Logistik", sendungsrichtung="ANKOMMEND"
    )
    outgoing = element(
        "X", fortschritt=3, sendungsname="Jane &amp; Co", sendungsrichtung="AUSGEHEND"
    )
    assert normalize_parcel(incoming, country="DE")["sender"] == "S & T Logistik"
    assert normalize_parcel(outgoing, country="DE")["receiver"] == "Jane & Co"


PACKSTATION_EVENT = (
    "Die Sendung liegt in der <a href='https://www.dhl.de/x?preferPackstation=true' "
    "class='arrowLink' target='_blank'><span class='arrow'></span>Packstation 216, "
    "Hauptstr. 1, 12345 Berlin</a> zur Abholung bereit."
)
PACKSTATION_ZUSTELLUNG = {
    "packageStationType": "PACKAGE_STATION",
    "directlyAddressed": True,
    "abholcodeAvailable": True,
}


def _at_packstation(**overrides):
    kwargs = {
        "fortschritt": 4,
        "ist_zugestellt": False,
        "zustellung": PACKSTATION_ZUSTELLUNG,
        "events": [
            {"datum": "2026-09-16T10:26:44+02:00", "status": "Die Sendung befindet sich auf dem Weg zur Packstation."},
            {"datum": "2026-09-16T15:05:08+02:00", "status": PACKSTATION_EVENT},
        ],
    }
    kwargs.update(overrides)
    return element("X", **kwargs)


def test_packstation_arrival_is_at_pickup_point_with_the_station_named():
    parcel = normalize_parcel(_at_packstation(), country="DE")
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Packstation 216, Hauptstr. 1, 12345 Berlin"


def test_pickup_point_is_unescaped_plain_text():
    event = "Bereit in <a href='x'><span class='arrow'></span>Packstation &amp; Co, 1</a> zur Abholung."
    parcel = normalize_parcel(
        _at_packstation(events=[{"datum": "d", "status": event}]), country="DE"
    )
    assert parcel["pickup_point"] == "Packstation & Co, 1"


def test_packstation_arrival_without_a_parsable_link_keeps_status_but_no_point():
    parcel = normalize_parcel(
        _at_packstation(events=[{"datum": "d", "status": "Bereit zur Abholung."}]),
        country="DE",
    )
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup_point"] is None


def test_out_for_delivery_needs_both_packstation_signals():
    for zustellung in (
        {"packageStationType": "PACKAGE_STATION", "abholcodeAvailable": False},
        {"abholcodeAvailable": True},
        {"directlyAddressed": True},
    ):
        parcel = normalize_parcel(_at_packstation(zustellung=zustellung), country="DE")
        assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
        assert parcel["pickup"] is False
        assert parcel["pickup_point"] is None


def test_collected_packstation_parcel_is_delivered_not_at_pickup_point():
    parcel = normalize_parcel(
        _at_packstation(fortschritt=5, ist_zugestellt=True), country="DE"
    )
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["pickup_point"] is None


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES


def test_capabilities_match_what_normalize_parcel_actually_returns():
    """Every declared CAPABILITIES entry must come true somewhere in a sample."""
    delivered = normalize_parcel(delivered_sample(), country="DE")
    active = normalize_parcel(active_sample(), country="DE")
    with_history = normalize_parcel(delivered_sample(), country="DE", include_history=True)

    if "weight" in CAPABILITIES:
        assert delivered["weight"] is not None
    if "dimensions" in CAPABILITIES:
        assert delivered["dimensions"] is not None
    if "delivery_window" in CAPABILITIES:
        assert active["planned_from"] is not None or active["planned_to"] is not None
    if "pickup_point" in CAPABILITIES:
        at_packstation = normalize_parcel(_at_packstation(), country="DE")
        assert at_packstation["pickup_point"] is not None
    if "url" in CAPABILITIES:
        assert delivered["url"] is not None
    if "history" in CAPABILITIES:
        assert with_history["history"] is not None


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels
