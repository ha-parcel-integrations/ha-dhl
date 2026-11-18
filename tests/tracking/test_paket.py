"""Tests for the DHL Paket Germany keyless backend: fetch only."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.dhl.const import DHLApiError
from custom_components.dhl.tracking.paket import (
    DHLPaketUnreachableError,
    async_fetch_paket,
    paket_shaped,
)
from tests.payloads import (
    ACTIVE_CODE,
    DELIVERED_CODE,
    delivered_sample,
    not_found_sample,
)


def _resp(status: int, body=None):
    resp = MagicMock()
    resp.status = status
    if isinstance(body, Exception):
        resp.json = AsyncMock(side_effect=body)
    else:
        resp.json = AsyncMock(return_value=body)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _session(*responses):
    session = MagicMock()
    session.get = MagicMock(side_effect=list(responses))
    return session


def _envelope(*elements, rate_limited: bool = False) -> dict:
    return {
        "sendungen": list(elements),
        "mergedAnonymousShipmentListIds": [],
        "rateLimited": rate_limited,
    }


def test_shape_needs_a_piece_code_length():
    assert paket_shaped(DELIVERED_CODE)
    assert paket_shaped("JJD000390007760000001")
    assert paket_shaped("340434161094")
    assert not paket_shaped("12345678901")
    assert not paket_shaped("JJD 0003900077600")


async def test_fetch_sends_the_piece_code_without_a_cookie_and_keys_on_id():
    session = _session(_resp(200, _envelope(delivered_sample())))
    result = await async_fetch_paket(session, [DELIVERED_CODE])
    assert set(result) == {DELIVERED_CODE}
    _, kwargs = session.get.call_args
    assert kwargs["params"]["piececode"] == DELIVERED_CODE
    assert kwargs["params"]["noRedirect"] == "true"
    assert "cookies" not in kwargs
    assert kwargs["allow_redirects"] is False


async def test_fetch_ignores_other_shipments_in_the_answer():
    session = _session(_resp(200, _envelope(delivered_sample(ACTIVE_CODE))))
    assert await async_fetch_paket(session, [DELIVERED_CODE]) == {}


async def test_fetch_not_found_element_is_no_answer():
    session = _session(_resp(200, _envelope(not_found_sample(DELIVERED_CODE))))
    assert await async_fetch_paket(session, [DELIVERED_CODE]) == {}


async def test_fetch_skips_codes_that_cannot_be_piece_codes():
    session = _session()
    assert await async_fetch_paket(session, ["SHORT"]) == {}
    session.get.assert_not_called()


@pytest.mark.parametrize(
    ("resp", "match"),
    [
        (_resp(303), "HTTP 303"),
        (_resp(200, ValueError("html")), "not a JSON object"),
    ],
)
async def test_fetch_a_foreign_answer_is_unreachable_not_a_miss(resp, match):
    with pytest.raises(DHLPaketUnreachableError, match=match):
        await async_fetch_paket(_session(resp), [DELIVERED_CODE])


@pytest.mark.parametrize(
    ("error", "match"),
    [(aiohttp.ClientError("boom"), "boom"), (TimeoutError(), "timed out")],
)
async def test_fetch_transport_failures_are_unreachable(error, match):
    session = MagicMock()
    session.get = MagicMock(side_effect=error)
    with pytest.raises(DHLPaketUnreachableError, match=match):
        await async_fetch_paket(session, [DELIVERED_CODE])


async def test_fetch_rate_limited_is_an_ordinary_error():
    session = _session(_resp(200, _envelope(rate_limited=True)))
    with pytest.raises(DHLApiError, match="rate limited") as err:
        await async_fetch_paket(session, [DELIVERED_CODE])
    assert not isinstance(err.value, DHLPaketUnreachableError)


async def test_fetch_unreachable_stops_the_rest_of_the_batch():
    codes = [f"JJD00039000776000000{i}" for i in range(6)]

    async def hang(*_):
        await asyncio.Event().wait()

    stalled = MagicMock()
    stalled.__aenter__ = AsyncMock(side_effect=hang)
    stalled.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(side_effect=[TimeoutError(), *[stalled] * 5])

    with pytest.raises(DHLPaketUnreachableError):
        await asyncio.wait_for(async_fetch_paket(session, codes), timeout=5)
    # The request in flight is cancelled; the queued codes are never sent.
    assert session.get.call_count < len(codes)


async def test_fetch_one_failing_code_keeps_the_others():
    session = _session(_resp(500), _resp(200, _envelope(delivered_sample())))
    result = await async_fetch_paket(session, [ACTIVE_CODE, DELIVERED_CODE])
    assert set(result) == {DELIVERED_CODE}


async def test_fetch_does_not_swallow_a_programming_error():
    session = MagicMock()
    session.get = MagicMock(side_effect=TypeError("bug"))
    with pytest.raises(TypeError):
        await async_fetch_paket(session, [DELIVERED_CODE])
