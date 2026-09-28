"""Tests for tracking/parcels.py's dispatcher and list helpers."""
from unittest.mock import MagicMock

import pytest

from custom_components.dhl.tracking import end_of_day
from custom_components.dhl.tracking.parcels import (
    BACKEND_KEY,
    apply_delivered_filter,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
    tracking_page_locale,
    tracking_page_url,
)

from .payloads import express_delivered, gateway_element


def test_normalize_dispatches_to_gateway():
    raw = {**gateway_element(barcode="A"), BACKEND_KEY: "gateway"}
    parcel = normalize_parcel(raw)
    assert parcel["barcode"] == "A"


def test_normalize_dispatches_to_express():
    raw = {**express_delivered("B"), BACKEND_KEY: "express"}
    parcel = normalize_parcel(raw)
    assert parcel["barcode"] == "B"


def test_normalize_strips_the_backend_marker_before_normalizing():
    """The backend marker must never leak into the raw payload a normalizer sees."""
    raw = {**gateway_element(barcode="A"), BACKEND_KEY: "gateway"}
    parcel = normalize_parcel(raw)
    assert BACKEND_KEY not in parcel["raw"]


def test_parse_iso_returns_none_for_unparseable_value():
    assert parse_iso("not a timestamp") is None


def test_parse_iso_returns_none_for_empty_value():
    assert parse_iso(None) is None
    assert parse_iso("") is None


def test_parse_iso_treats_naive_as_utc():
    parsed = parse_iso("2026-01-01T00:00:00")
    assert parsed.tzinfo is not None


def test_sort_parcels_by_ts_puts_missing_timestamps_last():
    parcels = [
        {"planned_from": None, "barcode": "no-ts"},
        {"planned_from": "2026-01-02T00:00:00Z", "barcode": "later"},
        {"planned_from": "2026-01-01T00:00:00Z", "barcode": "earlier"},
    ]
    sorted_parcels = sort_parcels_by_ts(parcels, "planned_from")
    assert [p["barcode"] for p in sorted_parcels] == ["earlier", "later", "no-ts"]


def test_apply_delivered_filter_by_days():
    entry = MagicMock()
    entry.options = {"delivered_filter_type": "days", "delivered_filter_amount": 1}
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    parcels = [
        {"barcode": "recent", "delivered_at": recent},
        {"barcode": "old", "delivered_at": old},
    ]
    filtered = apply_delivered_filter(parcels, entry)
    assert [p["barcode"] for p in filtered] == ["recent"]


def test_apply_delivered_filter_by_parcel_count():
    entry = MagicMock()
    entry.options = {"delivered_filter_type": "parcels", "delivered_filter_amount": 1}
    parcels = [{"barcode": "a"}, {"barcode": "b"}]
    filtered = apply_delivered_filter(parcels, entry)
    assert [p["barcode"] for p in filtered] == ["a"]


def test_dispatch_strips_only_the_backend_marker_from_raw():
    from custom_components.dhl.tracking.parcels import BACKEND_KEY, normalize_parcel

    from .payloads import express_delivered, gateway_element

    for payload, backend in ((gateway_element(), "gateway"), (express_delivered(), "express")):
        parcel = normalize_parcel({**payload, BACKEND_KEY: backend})
        assert parcel["raw"] == payload


@pytest.mark.parametrize(
    ("country", "language", "expected"),
    [
        ("NL", "nl", "nl-nl"),
        ("NL", "en", "nl-en"),
        ("NL", "de", "nl-en"),
        ("BE", "fr", "be-fr"),
        ("DE", "de-CH", "de-de"),
        ("US", "en", "us-en"),
        (None, "nl", "global-en"),
    ],
)
async def test_tracking_page_locale(hass, country, language, expected):
    hass.config.country = country
    hass.config.language = language
    assert tracking_page_locale(hass) == expected


def test_tracking_page_url_quotes_the_code():
    assert tracking_page_url("A B", "nl-nl") == (
        "https://www.dhl.com/nl-nl/home/tracking.html?tracking-id=A%20B"
    )


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        ("2026-09-24T13:05:00+02:00", "2026-09-24T23:59:59+02:00"),
        ("2026-07-07T21:59:00", "2026-07-07T23:59:59"),
        ("2026-09-22T22:15:00Z", "2026-09-22T23:59:59+00:00"),
        ("not a moment", None),
    ],
)
def test_end_of_day_keeps_the_day_and_its_offset(moment, expected):
    assert end_of_day(moment) == expected
