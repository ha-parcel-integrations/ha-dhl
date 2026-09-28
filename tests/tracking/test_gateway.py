"""Tests for tracking/gateway.py: batching, 404 handling, normalisation."""
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.dhl.const import HISTORY_MAX_EVENTS, ParcelStatus
from custom_components.dhl.tracking import gateway as gateway_module
from custom_components.dhl.tracking.gateway import (
    DHLGatewayError,
    async_fetch_gateway,
    normalize_parcel_gateway,
)

from .payloads import gateway_element


@pytest.fixture(autouse=True)
def _reset_one_shot_state():
    """Keep the unmapped-category one-shot WARNING dedup state per test."""
    gateway_module._warned_categories.clear()
    yield
    gateway_module._warned_categories.clear()


def _mock_session(*, status: int, json_body=None, text_body: str = ""):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_body)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=resp)
    return session, resp


async def test_empty_code_list_skips_the_request():
    session = MagicMock()
    assert await async_fetch_gateway(session, []) == {}
    session.get.assert_not_called()


async def test_batches_codes_into_one_comma_separated_request():
    session, resp = _mock_session(
        status=200, json_body=[gateway_element(barcode="A"), gateway_element(barcode="B")]
    )
    result = await async_fetch_gateway(session, ["A", "B"])

    assert set(result) == {"A", "B"}
    _, kwargs = session.get.call_args
    assert kwargs["params"] == {"key": "A,B"}


async def test_matches_on_barcode_field_not_position():
    """A mixed batch drops unknown codes silently — match by barcode, not index."""
    session, _ = _mock_session(status=200, json_body=[gateway_element(barcode="B")])
    result = await async_fetch_gateway(session, ["A", "B"])

    assert set(result) == {"B"}


async def test_404_is_not_found_not_an_error_and_never_json_parsed():
    session, resp = _mock_session(status=404)
    result = await async_fetch_gateway(session, ["UNKNOWN"])

    assert result == {}
    resp.json.assert_not_called()


async def test_connection_error_raises_gateway_error():
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientConnectionError("boom"))
    with pytest.raises(DHLGatewayError):
        await async_fetch_gateway(session, ["A"])


async def test_non_200_non_404_raises():
    session, _ = _mock_session(status=500)
    with pytest.raises(DHLGatewayError):
        await async_fetch_gateway(session, ["A"])


async def test_non_list_body_is_treated_as_no_results():
    session, _ = _mock_session(status=200, json_body={"unexpected": "shape"})
    assert await async_fetch_gateway(session, ["A"]) == {}


# ---------------------------------------------------------------------------
# normalize_parcel_gateway
# ---------------------------------------------------------------------------


def test_normalize_maps_category_to_status():
    raw = gateway_element(category="UNDERWAY", status="ARRIVED_AT_HUB")
    parcel = normalize_parcel_gateway(raw)

    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["raw_status"] == "ARRIVED_AT_HUB"
    assert parcel["carrier"] == "DHL"
    assert parcel["barcode"] == raw["barcode"]
    assert parcel["delivered"] is False


def test_normalize_delivered_flag_comes_from_delivered_at():
    raw = gateway_element(
        category="DELIVERED", status="DELIVERED", delivered_at="2026-02-13T15:02:22Z"
    )
    parcel = normalize_parcel_gateway(raw)

    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-02-13T15:02:22Z"


def test_returned_to_shipper_maps_to_returning_even_mid_log():
    """RETURNED_TO_SHIPPER is not guaranteed to be the last event's category,
    but its own `status` overrides whatever category it rode on — and the
    plan is explicit the log can resume with further UNDERWAY events after
    it, so this must key off the *last* event only."""
    raw = gateway_element(
        category="PROBLEM",
        status="RETURNED_TO_SHIPPER",
    )
    parcel = normalize_parcel_gateway(raw)

    assert parcel["status"] == ParcelStatus.RETURNING


def test_underway_event_after_returned_to_shipper_is_in_transit_not_returning():
    """A build must take the *last* event, not assume RETURNED_TO_SHIPPER
    ends the log once it has appeared anywhere in it."""
    raw = gateway_element(
        category="UNDERWAY",
        status="ARRIVED_AT_HUB",
        extra_events=[
            {
                "category": "PROBLEM",
                "status": "RETURNED_TO_SHIPPER",
                "type": "PIECE_EVENT",
                "timestamp": "2026-02-12T00:00:00Z",
                "leg": {"network": "ECOMMERCE"},
            }
        ],
    )
    parcel = normalize_parcel_gateway(raw)

    assert parcel["status"] == ParcelStatus.IN_TRANSIT


def test_unmapped_category_falls_back_to_unknown_and_warns_once(caplog):
    with caplog.at_level("WARNING"):
        parcel = normalize_parcel_gateway(
            gateway_element(category="SOMETHING_NEW", status="WEIRD")
        )
        assert parcel["status"] == ParcelStatus.UNKNOWN
        assert len(caplog.records) == 1

        normalize_parcel_gateway(gateway_element(category="SOMETHING_NEW", status="WEIRD2"))
        assert len(caplog.records) == 1  # second call is suppressed — one-shot per category


def test_planned_from_is_the_last_events_moment_indication():
    raw = gateway_element(moment="2026-02-13T13:00:00+01:00")
    parcel = normalize_parcel_gateway(raw)

    assert parcel["planned_from"] == "2026-02-13T13:00:00+01:00"
    assert parcel["planned_to"] is None


def test_a_delivered_parcel_has_no_plan_left():
    raw = gateway_element(
        category="DELIVERED",
        status="DELIVERED",
        delivered_at="2026-02-13T13:00:00+01:00",
    )
    parcel = normalize_parcel_gateway(raw)

    assert parcel["delivered_at"] == "2026-02-13T13:00:00+01:00"
    assert parcel["planned_from"] is None


def test_no_events_maps_to_unknown_without_crashing():
    parcel = normalize_parcel_gateway({"barcode": "X", "events": []})
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["barcode"] == "X"


def test_history_is_oldest_first_and_not_reversed():
    raw = gateway_element()
    parcel = normalize_parcel_gateway(raw, include_history=True)

    assert parcel["history"][0]["raw_status"] == "PRENOTIFICATION_RECEIVED"
    assert parcel["history"][-1]["raw_status"] == "OUT_FOR_DELIVERY"


def test_history_omitted_when_not_requested():
    parcel = normalize_parcel_gateway(gateway_element(), include_history=False)
    assert parcel["history"] is None


def test_never_present_fields_are_none():
    parcel = normalize_parcel_gateway(gateway_element())
    assert parcel["sender"] is None
    assert parcel["receiver"] is None
    assert parcel["pickup_point"] is None
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["url"] is None


def test_every_history_event_gets_its_own_status():
    raw = gateway_element(
        category="DELIVERED",
        status="DELIVERED",
        delivered_at="2026-02-13T13:00:00+01:00",
        extra_events=[
            {"category": "UNDERWAY", "status": "PARCEL_SORTED_AT_HUB", "timestamp": "2026-02-12T01:00:00Z"},
            {"category": "CUSTOMS", "status": "FACILITY_CHECK_IN", "timestamp": "2026-02-12T02:00:00Z"},
            {"category": "PROBLEM", "status": "NOT_HOME_NEW_DELIVERY", "timestamp": "2026-02-12T03:00:00Z"},
            {"category": "UNDERWAY", "status": "RETURNED_TO_SHIPPER", "timestamp": "2026-02-12T04:00:00Z"},
            {"category": "IN_DELIVERY", "status": "LOAD_VEHICLE", "timestamp": "2026-02-12T05:00:00Z"},
        ],
    )

    parcel = normalize_parcel_gateway(raw, include_history=True)

    assert [e["status"] for e in parcel["history"]] == [
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.PROBLEM,
        ParcelStatus.RETURNING,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.DELIVERED,
    ]


def test_an_unmapped_history_category_warns_once_and_maps_to_unknown(caplog):
    raw = gateway_element(
        extra_events=[
            {"category": "SOMETHING_NEW", "status": "X", "timestamp": "2026-02-12T01:00:00Z"},
            {"category": "SOMETHING_NEW", "status": "Y", "timestamp": "2026-02-12T02:00:00Z"},
        ],
    )

    parcel = normalize_parcel_gateway(raw, include_history=True)

    assert parcel["history"][1]["status"] == ParcelStatus.UNKNOWN
    assert parcel["history"][2]["status"] == ParcelStatus.UNKNOWN
    assert caplog.text.count("'SOMETHING_NEW'") == 1


def test_intervention_is_a_problem_unless_its_status_says_otherwise():
    reschedule = gateway_element(
        category="INTERVENTION",
        status="INTERVENTION_RECEIVER_REQUESTS_DELIVERY_AT_ANOTHER_TIME/DATE",
    )
    cancelled = gateway_element(
        category="INTERVENTION",
        status="INTERVENTION_RECEIVER_REQUEST_DELIVERY_CANCELLED",
    )
    bare = gateway_element(category="INTERVENTION", status="INTERVENTION")

    assert normalize_parcel_gateway(reschedule)["status"] == ParcelStatus.IN_TRANSIT
    assert normalize_parcel_gateway(cancelled)["status"] == ParcelStatus.RETURNING
    assert normalize_parcel_gateway(bare)["status"] == ParcelStatus.PROBLEM


def test_a_parcelshop_drop_off_leg_is_not_mistaken_for_a_return():
    for status in ("PARCEL_RETURNED_FROM_ROUTE", "PARCEL_READY_FOR_RETURN_TO_HUB"):
        raw = gateway_element(category="UNDERWAY", status=status)
        assert normalize_parcel_gateway(raw)["status"] == ParcelStatus.IN_TRANSIT


def test_a_collection_notice_is_at_pickup_point():
    raw = gateway_element(
        category="UNDERWAY",
        status="NOTIFICATION_FOR_PARCELSHOP_COLLECTION_HAS_BEEN_SENT",
    )
    assert normalize_parcel_gateway(raw)["status"] == ParcelStatus.AT_PICKUP_POINT


def test_raw_keeps_every_event_even_when_history_is_capped():
    extra = [
        {"category": "UNDERWAY", "status": "PARCEL_SORTED_AT_HUB", "timestamp": f"2026-02-12T{i:02d}:00:00Z"}
        for i in range(HISTORY_MAX_EVENTS + 5)
    ]
    raw = gateway_element(extra_events=extra)

    parcel = normalize_parcel_gateway(raw, include_history=True)

    assert parcel["raw"] == raw
    assert len(parcel["raw"]["events"]) == len(extra) + 2
    assert len(parcel["history"]) == HISTORY_MAX_EVENTS
