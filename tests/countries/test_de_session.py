"""Tests for the DHL Germany OIDC session (countries/de/session.py)."""
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest

from custom_components.dhl.countries.de.session import (
    DHLDeAuthError,
    DHLDeSession,
    DHLDeSessionError,
    decode_id_token_subject,
    generate_nonce,
    generate_pkce,
    generate_state,
)

DISCOVERY = {
    "authorization_endpoint": "https://login.dhl.de/x/login/authorize",
    "token_endpoint": "https://login.dhl.de/x/login/token",
}


def _response(status: int, body: dict | str) -> MagicMock:
    response = AsyncMock()
    response.status = status
    text = body if isinstance(body, str) else json.dumps(body)
    response.text = AsyncMock(return_value=text)
    if isinstance(body, dict):
        response.json = AsyncMock(return_value=body)
    else:
        response.json = AsyncMock(side_effect=ValueError("not json"))
    return response


def _ctx(response) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _session(
    *, discovery_status: int = 200, discovery_body: dict | None = None
) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(
        return_value=_ctx(_response(discovery_status, discovery_body or DISCOVERY))
    )
    return session


def _token_body(
    *,
    access_token: str = "access",
    id_token: str | None = "id",
    refresh_token: str | None = "refresh",
    expires_in: int = 1800,
) -> dict:
    body = {"access_token": access_token, "expires_in": expires_in}
    if id_token is not None:
        body["id_token"] = id_token
    if refresh_token is not None:
        body["refresh_token"] = refresh_token
    return body


def _jwt(sub: str | None) -> str:
    payload = {"sub": sub} if sub else {}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    return f"header.{encoded.decode()}.sig"


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_generate_pkce_matches_s256():
    verifier, challenge = generate_pkce()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    assert challenge == expected


def test_generate_pkce_is_fresh_every_call():
    """The ioBroker adapter's hardcoded verifier is exactly what not to do."""
    first, _ = generate_pkce()
    second, _ = generate_pkce()
    assert first != second


def test_generate_state_and_nonce_are_random():
    assert generate_state() != generate_state()
    assert generate_nonce() != generate_nonce()


def test_decode_id_token_subject():
    assert decode_id_token_subject(_jwt("abc123")) == "abc123"


def test_decode_id_token_subject_malformed_token():
    assert decode_id_token_subject("not-a-jwt") is None


def test_decode_id_token_subject_no_sub_claim():
    assert decode_id_token_subject(_jwt(None)) is None


def test_decode_id_token_subject_bad_base64():
    assert decode_id_token_subject("header.not-valid-base64!!!.sig") is None


# ---------------------------------------------------------------------------
# discovery + authorization URL
# ---------------------------------------------------------------------------


async def test_authorization_url_builds_from_discovery():
    session = _session()
    de_session = DHLDeSession(session)

    url, verifier, state = await de_session.async_authorization_url()

    parsed = urlparse(url)
    assert url.startswith(DISCOVERY["authorization_endpoint"])
    params = parse_qs(parsed.query)
    assert params["client_id"][0]
    assert params["state"][0] == state
    assert params["code_challenge_method"][0] == "S256"
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    assert params["code_challenge"][0] == expected_challenge


async def test_discovery_is_fetched_once():
    session = _session()
    de_session = DHLDeSession(session)

    await de_session.async_authorization_url()
    await de_session.async_authorization_url()

    assert session.get.call_count == 1


async def test_discovery_failure_raises():
    session = _session(discovery_status=500)
    with pytest.raises(DHLDeSessionError):
        await DHLDeSession(session).async_authorization_url()


async def test_discovery_missing_endpoints_raises():
    session = _session(discovery_body={"issuer": "x"})
    with pytest.raises(DHLDeSessionError):
        await DHLDeSession(session).async_authorization_url()


async def test_discovery_unparseable_body_raises():
    session = _session()
    session.get = MagicMock(return_value=_ctx(_response(200, "not json")))
    with pytest.raises(DHLDeSessionError):
        await DHLDeSession(session).async_authorization_url()


# ---------------------------------------------------------------------------
# code exchange
# ---------------------------------------------------------------------------


async def test_exchange_code_stores_tokens():
    session = _session()
    session.post = MagicMock(return_value=_ctx(_response(200, _token_body())))
    de_session = DHLDeSession(session)

    payload = await de_session.async_exchange_code("code123", "verifier")

    assert payload["access_token"] == "access"
    assert de_session.refresh_token == "refresh"
    assert de_session.id_token == "id"


async def test_exchange_code_without_refresh_token_raises():
    session = _session()
    session.post = MagicMock(
        return_value=_ctx(_response(200, _token_body(refresh_token=None)))
    )
    with pytest.raises(DHLDeSessionError):
        await DHLDeSession(session).async_exchange_code("code", "verifier")


async def test_exchange_code_invalid_grant_raises_auth_error():
    session = _session()
    session.post = MagicMock(
        return_value=_ctx(_response(400, {"error": "invalid_grant"}))
    )
    with pytest.raises(DHLDeAuthError):
        await DHLDeSession(session).async_exchange_code("bad-code", "verifier")


async def test_exchange_code_unauthorized_client_warns_once(caplog):
    session = _session()
    session.post = MagicMock(
        return_value=_ctx(_response(401, {"error": "unauthorized_client"}))
    )
    with pytest.raises(DHLDeAuthError):
        await DHLDeSession(session).async_exchange_code("code", "verifier")
    assert "retired" in caplog.text.lower()


async def test_exchange_code_outage_raises_session_error():
    session = _session()
    session.post = MagicMock(return_value=_ctx(_response(503, {})))
    with pytest.raises(DHLDeSessionError):
        await DHLDeSession(session).async_exchange_code("code", "verifier")


async def test_exchange_code_missing_id_token_raises():
    session = _session()
    body = _token_body(id_token=None)
    session.post = MagicMock(return_value=_ctx(_response(200, body)))
    with pytest.raises(DHLDeSessionError):
        await DHLDeSession(session).async_exchange_code("code", "verifier")


# ---------------------------------------------------------------------------
# get / refresh
# ---------------------------------------------------------------------------


async def test_get_id_token_without_refresh_token_raises():
    with pytest.raises(DHLDeSessionError):
        await DHLDeSession(_session()).async_get_id_token()


async def test_get_id_token_refreshes_when_missing():
    session = _session()
    session.post = MagicMock(return_value=_ctx(_response(200, _token_body())))
    de_session = DHLDeSession(session, refresh_token="stored-refresh")

    token = await de_session.async_get_id_token()

    assert token == "id"
    assert session.post.call_count == 1


async def test_get_id_token_reuses_cached_token_within_margin():
    session = _session()
    session.post = MagicMock(return_value=_ctx(_response(200, _token_body())))
    de_session = DHLDeSession(session, refresh_token="stored-refresh")
    await de_session.async_get_id_token()

    await de_session.async_get_id_token()

    assert session.post.call_count == 1  # not refreshed a second time


async def test_get_id_token_refreshes_inside_margin():
    session = _session()
    session.post = MagicMock(return_value=_ctx(_response(200, _token_body())))
    de_session = DHLDeSession(session, refresh_token="stored-refresh")
    await de_session.async_get_id_token()
    de_session._expires_at = datetime.now(timezone.utc) + timedelta(seconds=10)

    await de_session.async_get_id_token()

    assert session.post.call_count == 2


async def test_handle_unauthorized_forces_refresh():
    session = _session()
    session.post = MagicMock(return_value=_ctx(_response(200, _token_body())))
    de_session = DHLDeSession(session, refresh_token="stored-refresh")
    await de_session.async_get_id_token()

    await de_session.async_handle_unauthorized()

    assert session.post.call_count == 2


async def test_handle_unauthorized_without_refresh_token_raises():
    with pytest.raises(DHLDeSessionError):
        await DHLDeSession(_session()).async_handle_unauthorized()


async def test_refresh_token_rotation_is_flagged():
    session = _session()
    session.post = MagicMock(
        return_value=_ctx(_response(200, _token_body(refresh_token="rotated")))
    )
    de_session = DHLDeSession(session, refresh_token="original")

    await de_session.async_get_id_token()

    assert de_session.refresh_token == "rotated"
    assert de_session.pop_refresh_token_changed() is True
    assert de_session.pop_refresh_token_changed() is False


async def test_refresh_token_unchanged_is_not_flagged():
    session = _session()
    session.post = MagicMock(
        return_value=_ctx(_response(200, _token_body(refresh_token="same")))
    )
    de_session = DHLDeSession(session, refresh_token="same")

    await de_session.async_get_id_token()

    assert de_session.pop_refresh_token_changed() is False


async def test_refresh_missing_expires_in_falls_back_to_default():
    session = _session()
    body = _token_body()
    body["expires_in"] = "not-a-number"
    session.post = MagicMock(return_value=_ctx(_response(200, body)))
    de_session = DHLDeSession(session, refresh_token="stored-refresh")

    await de_session.async_get_id_token()

    assert de_session._expires_at is not None
