"""Tests for tracking/express.py: fetch, throttle detection, normalisation."""
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.dhl.const import (
    DHLApiError,
    DHLExpressCredentialError,
    DHLExpressThrottledError,
    ParcelStatus,
)
from custom_components.dhl.tracking import express as express_module
from custom_components.dhl.tracking.express import (
    _build_history,
    _checkpoint_timestamp,
    _derive_bearer_token,
    _edd_is_future,
    async_fetch_express,
    normalize_parcel_express,
)

from .payloads import express_delivered, express_in_transit


@pytest.fixture(autouse=True)
def _reset_one_shot_state():
    express_module._warned_statuses.clear()
    express_module._warned_checkpoints.clear()
    yield
    express_module._warned_statuses.clear()
    express_module._warned_checkpoints.clear()


def _mock_session(*, status: int, body: str):
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=body)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=resp)
    return session, resp


def test_bearer_token_derives_to_the_expected_shape():
    token = _derive_bearer_token()
    assert token.startswith("Bearer ")
    assert len(token) > len("Bearer ")


async def test_fetch_sends_the_derived_bearer_token_never_a_literal():
    session, resp = _mock_session(status=200, body=json.dumps([express_delivered()]))
    await async_fetch_express(session, "1000000001")

    _, kwargs = session.post.call_args
    assert kwargs["headers"]["Authorization"] == _derive_bearer_token()



async def test_fetch_names_the_service_before_the_method_in_the_url():
    # The backend 404s "Service URL params are not found/valid" on the
    # reversed order.
    session, resp = _mock_session(status=200, body=json.dumps([express_delivered()]))
    await async_fetch_express(session, "1000000001")

    url = session.post.call_args.args[0]
    assert "service=shipments-tracking" in url


async def test_connection_error_raises_api_error():
    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientConnectionError("boom"))
    with pytest.raises(DHLApiError):
        await async_fetch_express(session, "1000000001")


async def test_not_found_is_a_clean_empty_array():
    session, _ = _mock_session(status=200, body="[]")
    assert await async_fetch_express(session, "0000000000") is None


async def test_populated_array_returns_first_element():
    payload = express_delivered()
    session, _ = _mock_session(status=200, body=json.dumps([payload]))
    result = await async_fetch_express(session, payload["id"])
    assert result["id"] == payload["id"]


async def test_drg10012_in_body_raises_throttled_even_on_503():
    session, _ = _mock_session(status=503, body='{"error":"DRG10012"}')
    with pytest.raises(DHLExpressThrottledError):
        await async_fetch_express(session, "1000000001")


async def test_drg10012_in_body_raises_throttled_on_other_statuses_too():
    """Match on the body, never the status code alone — the mechanics
    research notes this code could in principle ride a different status."""
    session, _ = _mock_session(status=200, body='{"error":"DRG10012"}')
    with pytest.raises(DHLExpressThrottledError):
        await async_fetch_express(session, "1000000001")


async def test_401_raises_credential_error():
    session, _ = _mock_session(status=401, body="")
    with pytest.raises(DHLExpressCredentialError):
        await async_fetch_express(session, "1000000001")


async def test_403_raises_credential_error():
    session, _ = _mock_session(status=403, body="")
    with pytest.raises(DHLExpressCredentialError):
        await async_fetch_express(session, "1000000001")


async def test_other_error_status_raises_generic_api_error():
    session, _ = _mock_session(status=500, body="")
    with pytest.raises(DHLApiError):
        await async_fetch_express(session, "1000000001")


async def test_invalid_json_raises_api_error():
    session, _ = _mock_session(status=200, body="not json")
    with pytest.raises(DHLApiError):
        await async_fetch_express(session, "1000000001")


# ---------------------------------------------------------------------------
# normalize_parcel_express
# ---------------------------------------------------------------------------


def test_delivered_status_maps_to_delivered():
    parcel = normalize_parcel_express(express_delivered())
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["raw_status"] == "DELIVERED"
    assert parcel["carrier"] == "DHL"


def test_empty_status_with_checkpoints_and_future_edd_is_in_transit():
    parcel = normalize_parcel_express(express_in_transit())
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["delivered"] is False
    # Empty top-level status: the newest checkpoint stands in.
    assert parcel["raw_status"] == "In transit"


def test_empty_status_with_past_edd_is_unknown_not_in_transit():
    raw = express_in_transit()
    raw["eddDate"] = "2000-01-01"
    parcel = normalize_parcel_express(raw)
    assert parcel["status"] == ParcelStatus.UNKNOWN


def test_unmapped_status_warns_once(caplog):
    with caplog.at_level("WARNING"):
        raw = {"id": "X", "status": "SOMETHING_ELSE", "checkpoints": []}
        parcel = normalize_parcel_express(raw)
        assert parcel["status"] == ParcelStatus.UNKNOWN
        assert len(caplog.records) == 1

        normalize_parcel_express({"id": "Y", "status": "SOMETHING_ELSE", "checkpoints": []})
        assert len(caplog.records) == 1


def test_barcode_is_the_echoed_id():
    parcel = normalize_parcel_express(express_delivered(awb="9998887770"))
    assert parcel["barcode"] == "9998887770"


def test_history_is_reversed_to_oldest_first():
    parcel = normalize_parcel_express(express_delivered(), include_history=True)
    assert parcel["history"][0]["raw_status"] == "Picked up"
    assert parcel["history"][-1]["raw_status"] == "Delivered"


def test_history_omitted_when_not_requested():
    parcel = normalize_parcel_express(express_delivered(), include_history=False)
    assert parcel["history"] is None


def test_the_epod_link_stays_in_raw_not_url():
    parcel = normalize_parcel_express(express_delivered())
    assert parcel["url"] is None
    assert parcel["raw"]["signature"]["link"]["url"] == "https://example.invalid/pod"


def test_planned_from_combines_edd_date_and_permissive_time_formats():
    raw = express_in_transit()
    raw["eddDate"] = "2026-08-01"
    raw["eddTime"] = "1:07 PM"
    parcel = normalize_parcel_express(raw)
    assert parcel["planned_from"] == "2026-08-01T13:07:00+00:00"


def test_planned_from_falls_back_to_midnight_on_unparseable_time():
    raw = express_in_transit()
    raw["eddDate"] = "2026-08-01"
    raw["eddTime"] = "not a time"
    parcel = normalize_parcel_express(raw)
    assert parcel["planned_from"] == "2026-08-01T00:00:00+00:00"


def test_planned_from_falls_back_to_midnight_on_missing_time():
    raw = express_in_transit()
    raw["eddDate"] = "2026-08-01"
    raw["eddTime"] = ""
    parcel = normalize_parcel_express(raw)
    assert parcel["planned_from"] == "2026-08-01T00:00:00+00:00"


def test_planned_from_none_without_edd_date():
    raw = express_in_transit()
    del raw["eddDate"]
    assert normalize_parcel_express(raw)["planned_from"] is None


def test_edd_is_future_false_on_empty_or_unparseable_date():
    assert _edd_is_future(None) is False
    assert _edd_is_future("") is False
    assert _edd_is_future("not-a-date") is False


def test_checkpoint_timestamp_none_on_missing_or_unparseable_fields():
    assert _checkpoint_timestamp({}) is None
    assert _checkpoint_timestamp({"date": "Friday, July 31, 2026"}) is None
    assert _checkpoint_timestamp({"date": "garbage", "time": "13:07"}) is None


def test_build_history_skips_non_dict_and_unparseable_checkpoints():
    checkpoints = [
        "not a dict",
        {"description": "no date/time at all"},
        {
            "description": "Delivered",
            "time": "13:07",
            "date": "Friday, July 31, 2026",
        },
    ]
    history = _build_history(checkpoints)
    assert len(history) == 1
    assert history[0]["raw_status"] == "Delivered"


def test_never_present_fields_are_none():
    parcel = normalize_parcel_express(express_delivered())
    assert parcel["sender"] is None
    assert parcel["receiver"] is None
    assert parcel["pickup_point"] is None
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None


def test_a_delivered_parcel_has_its_delivery_moment_and_no_plan():
    parcel = normalize_parcel_express(express_delivered())
    # "EXAMPLE HUB" names no country, so the local time keeps no offset.
    assert parcel["delivered_at"] == "2026-07-31T13:07:00"
    assert parcel["planned_from"] is None
    assert parcel["raw_status"] == "DELIVERED"


def test_history_maps_known_checkpoints_and_warns_once_on_the_rest(caplog):
    parcel = normalize_parcel_express(express_delivered(), include_history=True)
    again = normalize_parcel_express(express_delivered(), include_history=True)

    assert [e["status"] for e in parcel["history"]] == [
        ParcelStatus.UNKNOWN,
        ParcelStatus.DELIVERED,
    ]
    assert again["history"] == parcel["history"]
    assert caplog.text.count("unrecognised checkpoint 'Picked up'") == 1
    assert "'Delivered'" not in caplog.text


_REAL_CHECKPOINTS = [
    # Newest-first, as the backend sends them.
    ("Shipment is out with courier for delivery", ParcelStatus.OUT_FOR_DELIVERY),
    ("Arrived at DHL Sort Facility  VALENCIA - SPAIN", ParcelStatus.IN_TRANSIT),
    ("Shipment has departed from a DHL facility VITORIA - SPAIN", ParcelStatus.IN_TRANSIT),
    ("Processed at VITORIA - SPAIN", ParcelStatus.IN_TRANSIT),
    ("Arrived at DHL Sort Facility  VITORIA - SPAIN", ParcelStatus.IN_TRANSIT),
    ("Shipment has departed from a DHL facility MILAN - MALPENSA - ITALY", ParcelStatus.IN_TRANSIT),
    ("Processed at MILAN - MALPENSA - ITALY", ParcelStatus.IN_TRANSIT),
    ("Shipment picked up", ParcelStatus.IN_TRANSIT),
    ("Shipment Accepted", ParcelStatus.IN_TRANSIT),
]


def _with_checkpoints(descriptions: list[str]) -> dict:
    raw = express_in_transit()
    raw["eddDate"] = "2000-01-01"
    raw["checkpoints"] = [
        {"description": d, "time": "09:00", "date": "Monday, July 27, 2026"}
        for d in descriptions
    ]
    return raw


def test_real_checkpoints_map_whatever_facility_they_name(caplog):
    raw = _with_checkpoints([d for d, _ in _REAL_CHECKPOINTS])
    parcel = normalize_parcel_express(raw, include_history=True)

    assert [e["status"] for e in parcel["history"]] == [
        s for _, s in reversed(_REAL_CHECKPOINTS)
    ]
    assert "unrecognised" not in caplog.text


def test_parcel_status_follows_the_newest_checkpoint():
    parcel = normalize_parcel_express(_with_checkpoints([d for d, _ in _REAL_CHECKPOINTS]))
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False


def test_an_unfetched_placeholder_is_unknown_without_a_warning(caplog):
    parcel = normalize_parcel_express({"id": "1", "status": "", "checkpoints": []})
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert "unrecognised" not in caplog.text


def test_an_unknown_checkpoint_warns_once_whatever_facility_it_names(caplog):
    normalize_parcel_express(
        _with_checkpoints(["Clearance delay VITORIA - SPAIN", "Clearance delay MILAN - ITALY"]),
        include_history=True,
    )
    assert caplog.text.count("unrecognised checkpoint") == 1


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("VALENCIA - Valencia - SPAIN", "2026-07-08T11:10:00+02:00"),
        ("BRNO - CZECH REPUBLIC, THE", "2026-07-08T11:10:00+02:00"),
        ("MILAN - MALPENSA - ITALY", "2026-07-08T11:10:00+02:00"),
        ("NEW YORK - USA", "2026-07-08T11:10:00"),
        (None, "2026-07-08T11:10:00"),
    ],
)
def test_checkpoint_time_is_local_to_the_country_it_names(location, expected):
    checkpoint = {"date": "Wednesday, July 08, 2026", "time": "11:10", "location": location}
    assert _checkpoint_timestamp(checkpoint) == expected


def test_later_real_checkpoints_map_too(caplog):
    raw = _with_checkpoints(
        [
            "Shipment is scheduled for delivery",
            "Delivery attempt could not be completed",
            "Further consignee information needed",
            "Arrived at DHL Delivery Facility  MONTEROTONDO - ITALY",
            "Delivery not accepted",
            "Shipment information received",
            "Customs clearance status updated. Note - The Customs clearance "
            "process may start while the shipment is in transit to the destination. ",
            "Shipment is in transit to destination",
            "On hold awaiting for payment of shipment related fees",
            "Clearance processing complete at CINCINNATI HUB - USA",
            "Payment is received and recorded for shipment related fees",
        ]
    )
    parcel = normalize_parcel_express(raw, include_history=True)

    assert [e["status"] for e in parcel["history"]] == [
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.PROBLEM,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.REGISTERED,
        ParcelStatus.PROBLEM,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.PROBLEM,
        ParcelStatus.PROBLEM,
        ParcelStatus.IN_TRANSIT,
    ]
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert "unrecognised" not in caplog.text
