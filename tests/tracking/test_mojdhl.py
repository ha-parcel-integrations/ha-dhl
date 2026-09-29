"""Tests for the Mój DHL public lookup backend: fetch and normalizer."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.dhl.const import DHLApiError, ParcelStatus
from custom_components.dhl.tracking.mojdhl import (
    async_fetch_mojdhl,
    normalize_parcel_mojdhl,
)

from .payloads import mojdhl_shipment

CODE = "31500000001"
CODE_2 = "JJD000000000000000001"
CHALLENGE = {
    "algorithm": "SHA-256",
    "challenge": "c",
    "salt": "s",
    "signature": "x",
    "maxnumber": 1,
}


@pytest.fixture(autouse=True)
def _solved():
    with patch(
        "custom_components.dhl.tracking.mojdhl.solve_altcha", return_value="solved"
    ):
        yield


def _response(status: int, body=None):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=body)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _session(*posts):
    """Each POST is preceded by its own challenge GET."""
    responses = []
    for status, body in posts:
        responses += [_response(200, CHALLENGE), _response(status, body)]
    session = MagicMock()
    session.request = MagicMock(side_effect=responses)
    return session


def _posted_bodies(session) -> list[dict]:
    return [
        call.kwargs["json"]
        for call in session.request.call_args_list
        if call.args[0] == "POST"
    ]


def test_delivered_parcel_maps_from_the_raw_code():
    parcel = normalize_parcel_mojdhl(mojdhl_shipment(CODE))

    assert parcel["barcode"] == CODE
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-09-22T09:51:00+00:00"
    assert parcel["raw_status"] == "Przesyłka została doręczona"
    assert parcel["sender"] == "EXAMPLE SENDER"
    assert parcel["history"] is None
    assert parcel["raw"]["internalStatus"] == "DRPDOR"


def test_active_parcel_carries_its_delivery_plan():
    parcel = normalize_parcel_mojdhl(
        mojdhl_shipment(
            CODE,
            status="TT_DWP",
            timelineStep="Delivery",
            receiptDateUtc=None,
            planOfDeliveryFromUtc="2026-09-22T06:00:00Z",
            planOfDeliveryToUtc="2026-09-22T14:00:00Z",
        )
    )

    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered_at"] is None
    assert parcel["planned_from"] == "2026-09-22T06:00:00+00:00"
    assert parcel["planned_to"] == "2026-09-22T14:00:00+00:00"


def test_waiting_in_a_locker_is_a_pickup():
    parcel = normalize_parcel_mojdhl(mojdhl_shipment(CODE, status="TT_LK"))
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True


def test_unknown_raw_code_falls_back_to_the_timeline_step_and_warns(caplog):
    parcel = normalize_parcel_mojdhl(
        mojdhl_shipment(CODE, status="TT_BRAND_NEW", timelineStep="Route")
    )
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert "TT_BRAND_NEW" in caplog.text


async def test_fetch_batches_every_code_and_keys_on_the_echoed_number():
    session = _session(
        (
            200,
            [
                {"number": CODE_2, "shipments": []},
                {"number": CODE, "shipments": [mojdhl_shipment(CODE)]},
            ],
        )
    )

    result = await async_fetch_mojdhl(session, [CODE, CODE_2])

    assert set(result) == {CODE}
    assert _posted_bodies(session) == [
        {"number1": CODE, "number2": CODE_2, "captcha-payload": "solved"}
    ]


async def test_fetch_never_sends_a_code_the_validator_would_reject_for_length():
    session = MagicMock()
    assert await async_fetch_mojdhl(session, ["1000000001"]) == {}
    session.request.assert_not_called()


async def test_a_rejected_number_is_dropped_and_the_rest_retried_once():
    session = _session(
        (422, {"errors": {"number2": ["invalid"]}, "code": 422}),
        (200, [{"number": CODE, "shipments": [mojdhl_shipment(CODE)]}]),
    )

    result = await async_fetch_mojdhl(session, [CODE, "ZZ-NOT-A-NUMBER"])

    assert set(result) == {CODE}
    assert [b.get("number2") for b in _posted_bodies(session)] == [
        "ZZ-NOT-A-NUMBER",
        None,
    ]


async def test_a_422_that_names_no_number_is_not_retried():
    session = _session((422, {"code": 422}))
    assert await async_fetch_mojdhl(session, [CODE]) == {}
    assert len(_posted_bodies(session)) == 1


async def test_fetch_non_200_raises():
    session = _session((500, None))
    with pytest.raises(DHLApiError):
        await async_fetch_mojdhl(session, [CODE])
