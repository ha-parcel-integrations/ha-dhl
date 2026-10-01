"""Tests for api/client.py: request shape and the status-code error mapping."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.dhl.api.client import (
    DHLUnifiedClient,
    DHLUnifiedError,
    DHLUnifiedKeyError,
    DHLUnifiedNotFound,
    DHLUnifiedRateLimitError,
    _retry_after,
)
from custom_components.dhl.const import DHL_UNIFIED_URL, DHLApiError

from .payloads import body, shipment


def _mock_session(*, status: int, json_body=None, headers=None, json_error=None):
    resp = MagicMock()
    resp.status = status
    resp.headers = headers or {}
    resp.json = AsyncMock(return_value=json_body, side_effect=json_error)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=resp)
    return session


async def test_sends_the_key_header_and_english_language():
    session = _mock_session(status=200, json_body=body(shipment()))
    result = await DHLUnifiedClient(session, "the-key").async_get_shipments("JJD1")

    assert result == body(shipment())
    args, kwargs = session.get.call_args
    assert args == (DHL_UNIFIED_URL,)
    assert kwargs["params"] == {"trackingNumber": "JJD1", "language": "en"}
    assert kwargs["headers"]["DHL-API-Key"] == "the-key"
    assert kwargs["timeout"].total == 30


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_key(status):
    session = _mock_session(status=status)
    with pytest.raises(DHLUnifiedKeyError):
        await DHLUnifiedClient(session, "k").async_get_shipments("X")


async def test_not_found_is_its_own_error():
    session = _mock_session(status=404)
    with pytest.raises(DHLUnifiedNotFound):
        await DHLUnifiedClient(session, "k").async_get_shipments("X")


async def test_rate_limit_carries_retry_after():
    session = _mock_session(status=429, headers={"Retry-After": "120"})
    with pytest.raises(DHLUnifiedRateLimitError) as err:
        await DHLUnifiedClient(session, "k").async_get_shipments("X")
    assert err.value.retry_after == 120


async def test_rate_limit_without_retry_after():
    session = _mock_session(status=429)
    with pytest.raises(DHLUnifiedRateLimitError) as err:
        await DHLUnifiedClient(session, "k").async_get_shipments("X")
    assert err.value.retry_after is None


async def test_other_status_is_a_generic_api_error():
    session = _mock_session(status=500)
    with pytest.raises(DHLUnifiedError) as err:
        await DHLUnifiedClient(session, "k").async_get_shipments("X")
    assert isinstance(err.value, DHLApiError)
    assert not isinstance(err.value, DHLUnifiedKeyError)


async def test_unparseable_body():
    session = _mock_session(status=200, json_error=ValueError("bad"))
    with pytest.raises(DHLUnifiedError):
        await DHLUnifiedClient(session, "k").async_get_shipments("X")


async def test_non_object_body():
    session = _mock_session(status=200, json_body=[])
    with pytest.raises(DHLUnifiedError):
        await DHLUnifiedClient(session, "k").async_get_shipments("X")


@pytest.mark.parametrize("error", [aiohttp.ClientError("boom"), TimeoutError()])
async def test_transport_failures(error):
    session = MagicMock()
    session.get = MagicMock(side_effect=error)
    with pytest.raises(DHLUnifiedError) as err:
        await DHLUnifiedClient(session, "k").async_get_shipments("X")
    assert not isinstance(err.value, DHLUnifiedKeyError)


def test_retry_after_parses_seconds_and_http_dates():
    assert _retry_after(None) is None
    assert _retry_after("") is None
    assert _retry_after("30") == 30
    assert _retry_after("-5") == 0
    assert _retry_after("not a date") is None
    later = datetime.now(timezone.utc) + timedelta(seconds=90)
    assert 60 < _retry_after(format_datetime(later, usegmt=True)) <= 90
    assert _retry_after("Wed, 21 Oct 2015 07:28:00") == 0
