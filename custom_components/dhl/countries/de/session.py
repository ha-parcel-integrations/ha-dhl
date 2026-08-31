"""The DHL Germany OIDC session: PKCE authorization, token exchange, refresh.

Akamai CIAM at ``login.dhl.de``, Authorization Code + PKCE, client
authenticated with an empty Basic-auth secret. This module owns the whole
token lifecycle:

- **Config-flow time**: :meth:`DHLDeSession.async_authorization_url` builds
  the one-time browser URL with a freshly generated PKCE verifier and
  ``state`` — never a fixed constant, a shared value would let one flow's
  code be replayed against another's. :meth:`async_exchange_code` trades the
  pasted-back ``code`` for tokens.
- **Every poll**, :meth:`async_get_id_token` refreshes the cached ID token
  once it is within :data:`DHL_DE_TOKEN_REFRESH_MARGIN_SECONDS` of expiry —
  never lazily on 401 alone, because a 30-minute token easily expires between
  a scheduled poll and the request actually going out after an HA sleep.
- **On a 401** from ``www.dhl.de``, :meth:`async_handle_unauthorized` forces
  exactly one refresh; the caller retries the failing request once and then
  gives up. This module never loops on its own.
- **A rejected refresh token** (the tenant's ``invalid_grant``/
  ``unauthorized_client``) raises :class:`DHLDeAuthError`, which the caller
  turns into ``ConfigEntryAuthFailed`` so Home Assistant prompts a reauth
  instead of retrying a token that will never work again — the same
  ``invalid_client``/401 shape also means the client itself was retired,
  which reauth is the correct response to either way.

Never hand the ID token out for any host other than ``www.dhl.de`` — that is
the caller's responsibility (countries/de/__init__.py), not enforced here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import aiohttp

from ...const import (
    DHL_DE_CLIENT_ID,
    DHL_DE_DISCOVERY_URL,
    DHL_DE_LOGIN_CLAIMS,
    DHL_DE_REDIRECT_URI,
    DHL_DE_REQUEST_TIMEOUT_SECONDS,
    DHL_DE_SCOPE,
    DHL_DE_TOKEN_HEADERS,
    DHL_DE_TOKEN_REFRESH_MARGIN_SECONDS,
    NEW_ISSUE_URL,
)

_LOGGER = logging.getLogger(__name__)

TOKEN_REFRESH_MARGIN = timedelta(seconds=DHL_DE_TOKEN_REFRESH_MARGIN_SECONDS)
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=DHL_DE_REQUEST_TIMEOUT_SECONDS)

# Fallback lifetime when a token response carries no `expires_in` at all —
# the confirmed live value (app-auth.md, 2026-08-17: `expires_in: 1800`).
_EXPECTED_TOKEN_LIFETIME = timedelta(seconds=1800)

_client_retirement_warned = False


class DHLDeSessionError(Exception):
    """Raised when the identity provider can't hand out a usable token."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        """Store the message and, where available, the HTTP status code."""
        super().__init__(message)
        self.status_code = status_code


class DHLDeAuthError(DHLDeSessionError):
    """Raised when the refresh token (or the client itself) is rejected.

    Distinct from :class:`DHLDeSessionError` so the caller can map only this
    one to ``ConfigEntryAuthFailed`` — a transport-level outage must not push
    a user into a reauth flow they cannot complete.
    """


def _warn_client_retirement_once(detail: str) -> None:
    """One-shot WARNING: a 401/invalid_client at the token endpoint (§7a).

    This is how the app's public client being switched off would present —
    worth a distinct, loud log line rather than blending into "reauth
    required".
    """
    global _client_retirement_warned
    if _client_retirement_warned:
        return
    _client_retirement_warned = True
    _LOGGER.warning(
        "DHL rejected the OIDC client itself (not just the refresh token) — "
        "this may mean the app client this integration relies on was "
        "retired. Open an issue: %s\n  detail=%s",
        NEW_ISSUE_URL,
        detail,
    )


def generate_pkce() -> tuple[str, str]:
    """Return a fresh ``(code_verifier, code_challenge)`` pair (S256).

    Generated per login, never reused — BUILD_PLAN.md §3 calls out the
    ioBroker adapter's hardcoded verifier by name as the thing not to copy.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def generate_state() -> str:
    """Return a fresh random ``state`` value for one authorization request."""
    return secrets.token_urlsafe(24)


def generate_nonce() -> str:
    """Return a fresh random ``nonce`` value for one authorization request."""
    return secrets.token_urlsafe(24)


def decode_id_token_subject(id_token: str) -> str | None:
    """Best-effort, unverified read of the ID token's ``sub`` claim.

    Used only to key ``unique_id`` at config-flow time — never to authorise
    anything, so no signature check is needed (or possible without the
    tenant's signing key). Returns ``None`` on any malformed token rather
    than raising, since a missing ``sub`` must not block the flow.
    """
    try:
        _, payload_b64, _ = id_token.split(".")
    except ValueError:
        return None
    padding = "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except (ValueError, UnicodeDecodeError):
        return None
    subject = payload.get("sub")
    return str(subject) if subject else None


class DHLDeSession:
    """Owns one config entry's OIDC tokens: discovery, exchange, refresh.

    One instance per config entry. Constructed with the entry's shared
    aiohttp session and, once past config-flow, the refresh token persisted
    in ``entry.data``. A fresh instance always starts with no cached ID
    token, so its first :meth:`async_get_id_token` call refreshes even
    though the refresh token itself is not new.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        refresh_token: str | None = None,
    ) -> None:
        """Initialise with an optional already-persisted refresh token."""
        self._session = session
        self.refresh_token = refresh_token
        self._id_token: str | None = None
        self._expires_at: datetime | None = None
        self._endpoints: dict[str, str] | None = None
        # Set when a refresh response carries a *different* refresh token
        # than the one we sent — some OIDC providers rotate it silently.
        # Cleared by `pop_refresh_token_changed()`, the coordinator's signal
        # to persist the new value in entry.data.
        self._refresh_token_changed = False

    @property
    def id_token(self) -> str | None:
        """The most recently cached ID token, or ``None`` before first use."""
        return self._id_token

    @property
    def _needs_refresh(self) -> bool:
        """Whether the cached ID token is missing, or inside the refresh margin."""
        if self._id_token is None or self._expires_at is None:
            return True
        return datetime.now(timezone.utc) >= self._expires_at - TOKEN_REFRESH_MARGIN

    def pop_refresh_token_changed(self) -> bool:
        """Return whether the refresh token rotated, and clear the flag."""
        value = self._refresh_token_changed
        self._refresh_token_changed = False
        return value

    async def _async_discover(self) -> dict[str, str]:
        """Fetch (and cache) the OIDC discovery document's endpoints.

        Fetched once per config entry / process, not once per request — the
        build plan calls this out explicitly as cheap insurance against a
        path move rather than something to hardcode.
        """
        if self._endpoints is not None:
            return self._endpoints
        async with self._session.get(
            DHL_DE_DISCOVERY_URL, timeout=_REQUEST_TIMEOUT
        ) as response:
            if response.status != 200:
                raise DHLDeSessionError(
                    f"discovery document returned HTTP {response.status}",
                    status_code=response.status,
                )
            try:
                document = await response.json(content_type=None)
            except ValueError as err:
                raise DHLDeSessionError(
                    f"discovery document was not valid JSON ({err})"
                ) from err
        try:
            self._endpoints = {
                "authorization_endpoint": document["authorization_endpoint"],
                "token_endpoint": document["token_endpoint"],
            }
        except (KeyError, TypeError) as err:
            raise DHLDeSessionError(
                "discovery document is missing authorization_endpoint/token_endpoint"
            ) from err
        return self._endpoints

    async def async_authorization_url(
        self,
    ) -> tuple[str, str, str]:
        """Build the one-time browser authorization URL.

        Returns ``(url, code_verifier, state)`` — the caller (config_flow.py)
        holds ``code_verifier`` and ``state`` for the lifetime of this one
        flow and passes ``code_verifier`` back into
        :meth:`async_exchange_code`.
        """
        endpoints = await self._async_discover()
        code_verifier, code_challenge = generate_pkce()
        state = generate_state()
        nonce = generate_nonce()
        params = {
            "response_type": "code",
            "client_id": DHL_DE_CLIENT_ID,
            "redirect_uri": DHL_DE_REDIRECT_URI,
            "scope": DHL_DE_SCOPE,
            "claims": DHL_DE_LOGIN_CLAIMS,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            "nonce": nonce,
            "prompt": "login",
        }
        query = "&".join(f"{key}={quote(value, safe='')}" for key, value in params.items())
        url = f"{endpoints['authorization_endpoint']}?{query}"
        return url, code_verifier, state

    async def async_exchange_code(
        self, code: str, code_verifier: str
    ) -> dict[str, Any]:
        """Exchange an authorization code for tokens; caches and returns them.

        The authorization code and PKCE verifier are single-use by design —
        the caller must discard them immediately after this call, whether it
        succeeds or fails.
        """
        endpoints = await self._async_discover()
        body = {
            "redirect_uri": DHL_DE_REDIRECT_URI,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
            "code": code,
        }
        payload = await self._async_post_token(endpoints["token_endpoint"], body)
        self._store_tokens(payload)
        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            raise DHLDeSessionError(
                "token response had no refresh_token — offline_access was "
                "not granted"
            )
        self.refresh_token = refresh_token
        return payload

    async def async_get_id_token(self) -> str:
        """Return a valid ID token for the tracking endpoint's ``dhli`` cookie."""
        if self.refresh_token is None:
            raise DHLDeSessionError(
                "no refresh_token to refresh with — the config entry is not set up"
            )
        if self._needs_refresh:
            await self._async_refresh()
        assert self._id_token is not None
        return self._id_token

    async def async_handle_unauthorized(self) -> str:
        """Force one refresh after a 401 from ``www.dhl.de``.

        Callers must retry the failing request exactly once with the
        returned token and then give up — never loop.
        """
        if self.refresh_token is None:
            raise DHLDeSessionError(
                "no refresh_token to refresh with — the config entry is not set up"
            )
        await self._async_refresh()
        assert self._id_token is not None
        return self._id_token

    async def _async_refresh(self) -> None:
        """``grant_type=refresh_token`` with an empty Basic-auth secret."""
        endpoints = await self._async_discover()
        body = {
            "redirect_uri": DHL_DE_REDIRECT_URI,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }
        payload = await self._async_post_token(endpoints["token_endpoint"], body)
        self._store_tokens(payload)
        new_refresh_token = payload.get("refresh_token")
        if new_refresh_token and new_refresh_token != self.refresh_token:
            self.refresh_token = new_refresh_token
            self._refresh_token_changed = True

    async def _async_post_token(
        self, url: str, body: dict[str, str]
    ) -> dict[str, Any]:
        """POST to the token endpoint; raises :class:`DHLDeAuthError` on rejection."""
        headers = {
            **DHL_DE_TOKEN_HEADERS,
            "Authorization": aiohttp.BasicAuth(DHL_DE_CLIENT_ID, "").encode(),
        }
        async with self._session.post(
            url, data=body, headers=headers, timeout=_REQUEST_TIMEOUT
        ) as response:
            text = await response.text()
            try:
                payload = json.loads(text) if text else {}
            except ValueError:
                payload = {}
            if response.status == 200 and isinstance(payload, dict) and payload.get(
                "access_token"
            ):
                return payload
            error = payload.get("error") if isinstance(payload, dict) else None
            if response.status in (400, 401) and error in (
                "invalid_grant",
                "unauthorized_client",
                "invalid_client",
            ):
                if error in ("unauthorized_client", "invalid_client"):
                    _warn_client_retirement_once(text)
                raise DHLDeAuthError(
                    f"DHL rejected the token request ({error or response.status})",
                    status_code=response.status,
                )
            raise DHLDeSessionError(
                f"token endpoint returned HTTP {response.status}",
                status_code=response.status,
            )

    def _store_tokens(self, payload: dict[str, Any]) -> None:
        """Cache the ID token and its expiry from a token-endpoint response."""
        id_token = payload.get("id_token")
        if not id_token:
            raise DHLDeSessionError("token response had no id_token")
        self._id_token = id_token
        expires_in = payload.get("expires_in")
        try:
            lifetime = timedelta(seconds=float(expires_in))
        except (TypeError, ValueError):
            lifetime = _EXPECTED_TOKEN_LIFETIME
        self._expires_at = datetime.now(timezone.utc) + lifetime


__all__ = [
    "DHLDeAuthError",
    "DHLDeSession",
    "DHLDeSessionError",
    "decode_id_token_subject",
    "generate_nonce",
    "generate_pkce",
    "generate_state",
]
