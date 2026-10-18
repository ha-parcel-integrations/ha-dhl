"""Tests for the DHL eCommerce Iberia backend: fetch and normalizer."""
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.dhl.const import DHLApiError, ParcelStatus
from custom_components.dhl.tracking.iberia import (
    async_fetch_iberia,
    iberia_shaped,
    normalize_parcel_iberia,
)

from .payloads import iberia_event, iberia_shipment

CODE = "JJD00026900000000000001"


def test_collected_at_the_service_point_is_delivered_there():
    parcel = normalize_parcel_iberia(iberia_shipment(), include_history=True)
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-08-18T16:46:00"
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "EXAMPLE SHOP"
    assert parcel["weight"] == 2.0
    assert parcel["raw_status"] == "Parcel picked up by recipient"
    assert parcel["history"][0] == {
        "timestamp": "2026-08-17T20:42:00",
        "status": ParcelStatus.REGISTERED,
        "raw_status": "Shipment data received",
    }
    assert parcel["history"][1]["raw_status"] == "Arrived at Valencia"
    assert parcel["history"][-1]["status"] == ParcelStatus.DELIVERED


def test_waiting_at_the_service_point_after_a_delivered_scan_is_at_pickup_point():
    events = [
        iberia_event("21/07/2026", "13:15", "IS", "Delivered to DHL ServicePoint"),
        iberia_event("21/07/2026", "13:09", "R", "Delivered"),
        iberia_event("21/07/2026", "10:22", "NH", "Consignee absent"),
    ]
    parcel = normalize_parcel_iberia(iberia_shipment(events=events, full=False))
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["delivered"] is False
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] is None
    assert parcel["weight"] is None


def test_a_round_after_a_delivered_scan_undoes_it():
    events = [
        iberia_event("15/10/2025", "12:00", "EC", "Delivery agreed with recipient 16/10/2025"),
        iberia_event("14/10/2025", "18:07", "PC", "Delivered, POD pending"),
    ]
    assert normalize_parcel_iberia(iberia_shipment(events=events))["status"] == (
        ParcelStatus.IN_TRANSIT
    )


def test_movement_after_a_return_scan_is_returning():
    events = [
        iberia_event("06/05/2026", "17:22", "ZZZ", "In transit in", town="Germany"),
        iberia_event("29/04/2026", "15:45", "RT", "Shipment being returned to sender"),
    ]
    parcel = normalize_parcel_iberia(iberia_shipment(events=events, full=False))
    assert parcel["status"] == ParcelStatus.RETURNING
    assert parcel["raw_status"] == "In transit in Germany"


def test_codeless_depot_scans_are_in_transit():
    events = [
        iberia_event("21/04/2026", "07:44", None, "Shipment received at DHL Depot"),
        iberia_event("16/07/2026", "18:02", None, "Picked up"),
    ]
    assert normalize_parcel_iberia(iberia_shipment(events=events))["status"] == (
        ParcelStatus.IN_TRANSIT
    )


def test_an_unknown_scan_warns_once_and_falls_back_to_the_previous_one(caplog):
    events = [
        iberia_event("02/06/2026", "10:00", "QQ", "Something new"),
        iberia_event("02/06/2026", "09:00", None, "Something else new"),
        iberia_event("01/06/2026", "09:00", "A", "Out for delivery"),
    ]
    parcel = normalize_parcel_iberia(iberia_shipment(events=events), include_history=True)
    normalize_parcel_iberia(iberia_shipment(events=events))
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["history"][-1]["status"] == ParcelStatus.UNKNOWN
    assert caplog.text.count("unrecognised scan (code QQ)") == 1
    assert caplog.text.count("unrecognised scan (text 'Something else new')") == 1


def test_only_unknown_scans_is_unknown():
    events = [iberia_event("02/06/2026", "10:00", "QQ", "Something new")]
    assert normalize_parcel_iberia(iberia_shipment(events=events))["status"] == (
        ParcelStatus.UNKNOWN
    )


def test_no_scans_is_registered_and_an_odd_clock_has_no_timestamp():
    assert normalize_parcel_iberia(iberia_shipment(events=[]))["status"] == (
        ParcelStatus.REGISTERED
    )
    events = [iberia_event("01/01/0001", "", "R", "Delivered")]
    parcel = normalize_parcel_iberia(iberia_shipment(events=events), include_history=True)
    assert parcel["delivered_at"] is None
    assert parcel["history"][0]["timestamp"] is None


@pytest.mark.parametrize("weight", ["0", "abc", None])
def test_a_missing_or_zero_weight_is_none(weight):
    assert normalize_parcel_iberia(iberia_shipment(Weight=weight))["weight"] is None


def test_never_an_empty_number():
    assert not iberia_shaped("")
    assert not iberia_shaped("JJD 123")
    assert iberia_shaped("5001191987")


def _resp(status: int, body=None, content_type: str = "application/json"):
    resp = MagicMock()
    resp.status = status
    resp.content_type = content_type
    resp.json = AsyncMock(return_value=body)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _session(*responses):
    session = MagicMock()
    session.get = MagicMock(side_effect=list(responses))
    return session


async def test_fetch_keys_answers_by_code_and_sends_the_number_only():
    session = _session(_resp(200, iberia_shipment(CODE)), _resp(204, None, ""))
    result = await async_fetch_iberia(session, [CODE, "2800000007"])
    assert set(result) == {CODE}
    _, kwargs = session.get.call_args_list[0]
    assert kwargs["params"] == {"number": CODE}


async def test_fetch_skips_the_request_for_an_empty_code():
    session = _session()
    assert await async_fetch_iberia(session, [""]) == {}
    session.get.assert_not_called()


@pytest.mark.parametrize("status", [204, 404, 409])
async def test_fetch_not_found_statuses_are_no_answer(status):
    resp = _resp(status)
    assert await async_fetch_iberia(_session(resp), [CODE]) == {}
    resp.json.assert_not_called()


async def test_fetch_ignores_an_answer_for_another_number():
    session = _session(_resp(200, iberia_shipment("JJD0OTHER")), _resp(200, ["junk"]))
    assert await async_fetch_iberia(session, [CODE, "JJD0SECOND"]) == {}


async def test_fetch_a_firewall_page_under_200_is_an_error_not_a_miss():
    session = _session(_resp(200, None, "text/html"))
    with pytest.raises(DHLApiError, match="text/html"):
        await async_fetch_iberia(session, [CODE])


async def test_fetch_one_failing_code_keeps_the_others():
    session = _session(_resp(500), _resp(200, iberia_shipment(CODE)))
    result = await async_fetch_iberia(session, ["JJD0FAILING", CODE])
    assert set(result) == {CODE}


async def test_fetch_raises_when_every_request_fails():
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(DHLApiError, match="boom"):
        await async_fetch_iberia(session, [CODE, "JJD0SECOND"])


async def test_fetch_does_not_swallow_a_programming_error():
    session = MagicMock()
    session.get = MagicMock(side_effect=TypeError("bug"))
    with pytest.raises(TypeError):
        await async_fetch_iberia(session, [CODE])
