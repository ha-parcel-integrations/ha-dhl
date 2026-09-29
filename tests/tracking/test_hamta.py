"""Tests for the DHL Freight Sweden (Mitt DHL) backend: fetch and normalizer."""
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.dhl.const import DHLApiError, ParcelStatus
from custom_components.dhl.tracking.hamta import (
    async_fetch_hamta,
    normalize_parcel_hamta,
)

from .payloads import hamta_shipment

_DROPPED = (21, 0, "DELIVERED OK", "2026-09-25T11:08:00.000Z")
_NOTIFIED = (20, 907, "RECEIVER NOTIFIED SERVICE POINT", "2026-09-25T11:55:00.000Z")
_COLLECTED = (21, 908, "DELIVERED BY SERVICE POINT", "2026-09-25T17:16:00.000Z")
_PROCESSED = (24, 0, "PROCESSED AT TERMINAL", "2026-09-24T16:31:00.000Z")


def test_out_for_delivery_follows_the_newest_event():
    parcel = normalize_parcel_hamta(hamta_shipment())
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["raw_status"] == "OUT FOR DELIVERY"
    assert parcel["delivered"] is False
    assert parcel["sender"] == "EXAMPLE SENDER"
    assert parcel["receiver"] is None
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "EXAMPLE SHOP"


def test_no_events_is_registered():
    assert normalize_parcel_hamta(hamta_shipment(events=[]))["status"] == (
        ParcelStatus.REGISTERED
    )


def test_dropped_at_the_service_point_is_at_pickup_point():
    parcel = normalize_parcel_hamta(
        hamta_shipment(events=[_PROCESSED, _DROPPED, _NOTIFIED], isReadyForCollection=True)
    )
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["delivered"] is False


def test_collected_is_delivered_at_the_hand_over_not_the_drop_off():
    parcel = normalize_parcel_hamta(
        hamta_shipment(
            events=[_PROCESSED, _DROPPED, _NOTIFIED, _COLLECTED], isCollected=True
        ),
        include_history=True,
    )
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered_at"] == "2026-09-25T17:16:00.000Z"
    assert [e["status"] for e in parcel["history"]] == [
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.DELIVERED,
    ]


def test_home_delivery_hands_over_on_the_plain_delivered_event():
    parcel = normalize_parcel_hamta(
        hamta_shipment(
            delivery_method="HOME_DELIVERY",
            events=[_PROCESSED, _DROPPED],
            isCollected=True,
        )
    )
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered_at"] == "2026-09-25T11:08:00.000Z"
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None


def test_timed_out_at_the_service_point_is_returning():
    parcel = normalize_parcel_hamta(
        hamta_shipment(events=[_PROCESSED, _DROPPED], isTimeout=True)
    )
    assert parcel["status"] == ParcelStatus.RETURNING


def test_unknown_event_warns_once_and_keeps_the_parcel_in_transit(caplog):
    odd = (77, 1, "SOMETHING NEW", "2026-09-25T09:00:00.000Z")
    parcel = normalize_parcel_hamta(
        hamta_shipment(events=[_PROCESSED, odd]), include_history=True
    )
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["history"][-1]["status"] == ParcelStatus.UNKNOWN
    assert caplog.text.count("unrecognised event") == 1


def test_events_are_ordered_oldest_first_whatever_the_feed_order():
    parcel = normalize_parcel_hamta(
        hamta_shipment(events=[_COLLECTED, _PROCESSED]), include_history=True
    )
    assert [e["raw_status"] for e in parcel["history"]] == [
        "PROCESSED AT TERMINAL",
        "DELIVERED BY SERVICE POINT",
    ]


def _mock_session(*, status: int, json_body=None):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_body)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=resp)
    return session, resp


async def test_fetch_skips_the_request_without_a_hamta_shaped_code():
    session = MagicMock()
    assert await async_fetch_hamta(session, ["JJD000", "123"]) == {}
    session.get.assert_not_called()


async def test_fetch_keys_shipments_by_tracking_number():
    session, _ = _mock_session(
        status=200, json_body=[hamta_shipment("8000000002"), {"no": "number"}, "junk"]
    )
    result = await async_fetch_hamta(session, ["8000000001", "8000000002"])

    assert set(result) == {"8000000002"}
    _, kwargs = session.get.call_args
    assert kwargs["params"] == [("ids", "8000000001"), ("ids", "8000000002")]


async def test_fetch_404_is_not_found_not_an_error():
    session, resp = _mock_session(status=404)
    assert await async_fetch_hamta(session, ["8000000001"]) == {}
    resp.json.assert_not_called()


async def test_fetch_non_200_raises():
    session, _ = _mock_session(status=500)
    with pytest.raises(DHLApiError):
        await async_fetch_hamta(session, ["8000000001"])


async def test_fetch_connection_error_raises():
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientConnectionError("boom"))
    with pytest.raises(DHLApiError):
        await async_fetch_hamta(session, ["8000000001"])


async def test_fetch_non_list_body_is_empty():
    session, _ = _mock_session(status=200, json_body={"error": "x"})
    assert await async_fetch_hamta(session, ["8000000001"]) == {}


def test_return_to_sender_event_is_returning():
    parcel = normalize_parcel_hamta(
        hamta_shipment(events=[(56, 909, "RETURNED", "2026-09-26T10:00:00.000Z")])
    )
    assert parcel["status"] == ParcelStatus.RETURNING


def test_timed_out_shipment_is_returning():
    assert normalize_parcel_hamta(hamta_shipment(isTimeout=True))["status"] == (
        ParcelStatus.RETURNING
    )
