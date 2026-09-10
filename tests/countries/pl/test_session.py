"""Tests for the Mój DHL (Poland) session lifecycle (countries/pl/session.py)."""
from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.dhl.const import DHLApiError, DHLAuthError
from custom_components.dhl.countries.pl.session import (
    DHLPlSession,
    new_device_id,
    solve_altcha,
)

CHALLENGE = {
    "algorithm": "SHA-256", "salt": "test-salt", "signature": "sig",
    "maxnumber": 50,
    "challenge": hashlib.sha256(b"test-salt7").hexdigest(),
}


def _response(status: int, body) -> AsyncMock:
    response = AsyncMock()
    response.status = status
    response.json = AsyncMock(return_value=body)
    return response


def _ctx(response) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _session(*responses) -> MagicMock:
    """A session whose cookie jar is real (round-trips) but whose HTTP calls
    are canned, one context manager per call in order."""
    session = MagicMock()
    session.cookie_jar = aiohttp.CookieJar()
    session.request = MagicMock(side_effect=[_ctx(r) for r in responses])
    return session


def test_new_device_id_returns_a_stable_looking_uuid():
    device_id = new_device_id()
    assert isinstance(device_id, str)
    assert device_id != new_device_id()


def test_solve_altcha_raises_on_malformed_challenge():
    with pytest.raises(DHLApiError):
        solve_altcha({"algorithm": "SHA-256"})


def test_solve_altcha_raises_when_unsolvable_within_maxnumber():
    challenge = {**CHALLENGE, "challenge": "not-a-real-hash", "maxnumber": 2}
    with pytest.raises(DHLApiError):
        solve_altcha(challenge)


async def test_send_sms_returns_lockout_seconds():
    session = _session(
        _response(200, CHALLENGE),
        _response(200, {"status": 0, "basicRegulationAccepted": False}),
        _response(200, CHALLENGE),
        _response(200, {"responseCode": 0, "lockoutSeconds": 60, "failedAttempts": 1}),
    )
    pl_session = DHLPlSession(session)

    lockout = await pl_session.async_send_sms("501234567")

    assert lockout == 60


async def test_send_sms_raises_when_declined():
    session = _session(
        _response(200, CHALLENGE),
        _response(200, {"status": 0}),
        _response(200, CHALLENGE),
        _response(200, {"responseCode": 1, "lockoutSeconds": 60}),
    )
    pl_session = DHLPlSession(session)

    with pytest.raises(DHLApiError):
        await pl_session.async_send_sms("501234567")


async def test_send_sms_raises_on_http_error():
    session = _session(_response(200, CHALLENGE), _response(422, {"errors": {}}))
    pl_session = DHLPlSession(session)

    with pytest.raises(DHLApiError):
        await pl_session.async_send_sms("501234567")


def _jwt(header: str = "header", payload: str = "payload", signature: str = "signature") -> str:
    return f"{header}.{payload}.{signature}"


async def test_verify_sms_adopts_the_split_access_token():
    session = _session(
        _response(200, CHALLENGE),
        _response(200, {"success": True, "token": {"token": _jwt()}}),
    )
    pl_session = DHLPlSession(session)

    await pl_session.async_verify_sms("501234567", "123456", "device-1")

    exported = {c["name"]: c["value"] for c in pl_session.export_cookies()}
    assert exported["access-token"] == "header.payload"
    assert exported["access-signature"] == "signature"


async def test_verify_sms_raises_dhl_auth_error_when_rejected():
    session = _session(_response(200, CHALLENGE), _response(200, {"success": False}))
    pl_session = DHLPlSession(session)

    with pytest.raises(DHLAuthError):
        await pl_session.async_verify_sms("501234567", "000000", "device-1")


async def test_verify_sms_raises_on_non_jwt_token():
    session = _session(
        _response(200, CHALLENGE),
        _response(200, {"success": True, "token": {"token": "not-a-jwt"}}),
    )
    pl_session = DHLPlSession(session)

    with pytest.raises(DHLApiError):
        await pl_session.async_verify_sms("501234567", "123456", "device-1")


async def test_refresh_adopts_a_fresh_token_and_returns_it():
    session = _session(_response(200, {"token": _jwt("h2", "p2", "s2")}))
    pl_session = DHLPlSession(session)

    token = await pl_session.async_refresh("device-1")

    assert token == "h2.p2.s2"
    exported = {c["name"]: c["value"] for c in pl_session.export_cookies()}
    assert exported["access-token"] == "h2.p2"
    assert exported["access-signature"] == "s2"


async def test_refresh_raises_dhl_auth_error_on_401():
    session = _session(_response(401, {}))
    pl_session = DHLPlSession(session)

    with pytest.raises(DHLAuthError):
        await pl_session.async_refresh("device-1")


async def test_refresh_raises_dhl_api_error_on_other_failure():
    session = _session(_response(500, {}))
    pl_session = DHLPlSession(session)

    with pytest.raises(DHLApiError):
        await pl_session.async_refresh("device-1")


async def test_import_cookies_ignores_malformed_entries():
    session = MagicMock()
    session.cookie_jar = aiohttp.CookieJar()
    pl_session = DHLPlSession(session, [{"name": "only-a-name"}])

    assert pl_session.export_cookies() == []


async def test_export_cookies_only_returns_mojdhl_domain():
    session = MagicMock()
    session.cookie_jar = aiohttp.CookieJar()
    pl_session = DHLPlSession(session)
    pl_session.import_cookies([
        {"name": "access-token", "value": "v", "domain": "mojdhl.pl", "path": "/", "secure": True},
        {"name": "unrelated", "value": "v", "domain": "example.com", "path": "/", "secure": True},
    ])

    domains = {c["domain"] for c in pl_session.export_cookies()}

    assert domains == {"mojdhl.pl"}


async def test_aclose_closes_the_underlying_session():
    session = MagicMock()
    session.cookie_jar = aiohttp.CookieJar()
    session.close = AsyncMock()
    pl_session = DHLPlSession(session)

    await pl_session.aclose()

    session.close.assert_awaited_once()
