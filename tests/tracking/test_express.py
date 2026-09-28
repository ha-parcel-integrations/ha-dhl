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
    assert parcel["raw_status"] == ""


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


def test_url_from_epod_signature():
    parcel = normalize_parcel_express(express_delivered())
    assert parcel["url"] == "https://example.invalid/pod"


def test_url_none_when_signature_missing_or_not_epod():
    raw = express_delivered()
    raw["signature"] = {"type": "something_else"}
    assert normalize_parcel_express(raw)["url"] is None

    raw2 = express_delivered()
    del raw2["signature"]
    assert normalize_parcel_express(raw2)["url"] is None


def test_planned_from_combines_edd_date_and_permissive_time_formats():
    raw = express_delivered()
    raw["eddTime"] = "1:07 PM"
    parcel = normalize_parcel_express(raw)
    assert parcel["planned_from"] == "2026-08-01T13:07:00"


def test_planned_from_falls_back_to_midnight_on_unparseable_time():
    raw = express_delivered()
    raw["eddTime"] = "not a time"
    parcel = normalize_parcel_express(raw)
    assert parcel["planned_from"] == "2026-08-01T00:00:00"


def test_planned_from_falls_back_to_midnight_on_missing_time():
    raw = express_delivered()
    raw["eddTime"] = ""
    parcel = normalize_parcel_express(raw)
    assert parcel["planned_from"] == "2026-08-01T00:00:00"


def test_planned_from_none_without_edd_date():
    raw = express_delivered()
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
    assert parcel["delivered_at"] is None


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
