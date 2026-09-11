"""Mój DHL (Poland) SMS session and Altcha enrolment helpers."""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from http.cookies import SimpleCookie
from typing import Any

import aiohttp

from ...const import (
    DHL_PL_BASE_URL,
    DHL_PL_HEADERS,
    DHL_PL_REQUEST_TIMEOUT_SECONDS,
    DHLApiError,
    DHLAuthError,
)

_TIMEOUT = aiohttp.ClientTimeout(total=DHL_PL_REQUEST_TIMEOUT_SECONDS)

# Sent at login and on every refresh/recover. Must stay stable for the life of
# a config entry: DHL binds the remember-me credential to the device pair, and
# it identifies the integration rather than the user's installation.
DEVICE_NAME = "Home Assistant"


def new_device_id() -> str:
    """Return a stable opaque device id for one config entry."""
    return str(uuid.uuid4())


def solve_altcha(challenge: dict[str, Any]) -> str:
    """Solve DHL's bounded SHA-256 proof of work without blocking HTTP I/O."""
    try:
        salt, target = challenge["salt"], challenge["challenge"]
        maximum = int(challenge["maxnumber"])
        algorithm, signature = challenge["algorithm"], challenge["signature"]
    except (KeyError, TypeError, ValueError) as err:
        raise DHLApiError("invalid Altcha challenge") from err
    for number in range(maximum + 1):
        if hashlib.sha256(f"{salt}{number}".encode()).hexdigest() == target:
            payload = {
                "algorithm": algorithm,
                "challenge": target,
                "number": number,
                "salt": salt,
                "signature": signature,
            }
            return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    raise DHLApiError("Altcha challenge could not be solved within maxnumber")


class DHLPlSession:
    """Owns Mój DHL's cookie credential and the rotating access-token pair."""

    def __init__(self, session: aiohttp.ClientSession, cookies: list[dict] | None = None) -> None:
        """Wrap an aiohttp session, seeding its jar from a stored cookie snapshot."""
        self._session = session
        self.import_cookies(cookies or [])

    def import_cookies(self, cookies: list[dict]) -> None:
        """Load a stored cookie snapshot into the session's live jar."""
        for item in cookies:
            if not all(isinstance(item.get(key), str) for key in ("name", "value", "domain")):
                continue
            cookie = SimpleCookie()
            cookie[item["name"]] = item["value"]
            morsel = cookie[item["name"]]
            morsel["domain"] = item["domain"]
            morsel["path"] = item.get("path", "/")
            if item.get("secure", True):
                morsel["secure"] = True
            self._session.cookie_jar.update_cookies(cookie)

    def export_cookies(self) -> list[dict[str, Any]]:
        """Serialize all cookies, including session cookies, for HA storage."""
        return [
            {"name": cookie.key, "value": cookie.value, "domain": cookie["domain"],
             "path": cookie["path"] or "/", "secure": bool(cookie["secure"])}
            for cookie in self._session.cookie_jar
            if cookie["domain"].endswith("mojdhl.pl")
        ]

    def _adopt_access_token(self, token: str) -> None:
        """Store a minted JWT in DHL's two-cookie representation.

        `/auth/refresh` does not itself replace these cookies.  Replacing both
        is what makes the server's 30-minute session window slide.
        """
        parts = token.split(".")
        if len(parts) != 3:
            raise DHLApiError("Mój DHL refresh returned a non-JWT token")
        cookie = SimpleCookie()
        cookie["access-token"] = ".".join(parts[:2])
        cookie["access-signature"] = parts[2]
        for morsel in cookie.values():
            morsel["domain"] = "mojdhl.pl"
            morsel["path"] = "/"
            morsel["secure"] = True
        self._session.cookie_jar.update_cookies(cookie)

    async def _json(self, method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
        headers = {**DHL_PL_HEADERS, **kwargs.pop("headers", {})}
        async with self._session.request(
            method, f"{DHL_PL_BASE_URL}{path}", headers=headers, timeout=_TIMEOUT, **kwargs
        ) as response:
            try:
                return response.status, await response.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                return response.status, None

    async def _captcha(self) -> str:
        status, challenge = await self._json("GET", "/auth/captcha/challenge")
        if status != 200 or not isinstance(challenge, dict):
            raise DHLApiError(f"Altcha challenge request failed: HTTP {status}")
        return solve_altcha(challenge)

    async def async_send_sms(self, phone: str) -> int:
        """Validate a phone and request exactly one SMS; return DHL's lockout."""
        for path, body in (
            ("/auth/validate-account", {"phoneNumber": phone, "prefix": "48"}),
            ("/auth/generate-code", {"phoneNumber": phone, "prefix": "48", "isMobileDevice": False}),
        ):
            body["captcha-payload"] = await self._captcha()
            status, response = await self._json("POST", path, json=body)
            if status != 200 or not isinstance(response, dict):
                raise DHLApiError(f"Mój DHL {path} failed: HTTP {status}")
        if response.get("responseCode") != 0:
            raise DHLApiError("Mój DHL declined the SMS request")
        return int(response.get("lockoutSeconds") or 60)

    async def async_verify_sms(self, phone: str, sms_code: str, device_id: str) -> None:
        """Redeem the user-entered SMS code and adopt the returned access token."""
        # rememberMe buys the durable `access-remember` cookie that
        # `_async_recover` later trades for a fresh token.
        body = {"phoneNumber": phone, "prefix": "48", "smsCode": sms_code,
                "deviceId": device_id, "deviceName": DEVICE_NAME,
                "rememberMe": True, "captcha-payload": await self._captcha()}
        status, response = await self._json("POST", "/auth/validate-code", json=body)
        token = response.get("token", {}).get("token") if isinstance(response, dict) else None
        if status != 200 or not response.get("success") or not isinstance(token, str):
            raise DHLAuthError("Mój DHL rejected the SMS code")
        self._adopt_access_token(token)

    async def aclose(self) -> None:
        """Close the underlying session — only for a config-flow-owned instance.

        The runtime session (owned by ``__init__.py``'s ``DHLData``) is closed
        by Home Assistant's own connector teardown and must never be closed
        here; this is only for the throwaway session a config flow creates to
        carry cookies across its phone/SMS steps.
        """
        await self._session.close()

    async def _async_recover(self, device_id: str) -> str:
        """Restore a session whose access token already expired.

        ``/auth/refresh`` authenticates *with* the access-token cookie pair,
        so it cannot help once that token is past its 30-minute life — which
        is every time Home Assistant is down longer than that. ``/auth/recover``
        takes the long-lived ``access-remember`` cookie instead (issued by
        ``rememberMe: true`` at login) and needs no Altcha, so only a revoked
        or expired remember cookie sends the user back through SMS.

        A transport failure must not be mistaken for a dead credential: only
        an outright rejection raises :class:`DHLAuthError`.
        """
        status, response = await self._json(
            "POST", "/auth/recover", json={"deviceId": device_id, "deviceName": DEVICE_NAME}
        )
        token = response.get("token") if isinstance(response, dict) else None
        if status in (400, 401, 403):
            raise DHLAuthError("Mój DHL session expired and could not be recovered")
        if status != 200 or not isinstance(token, str):
            raise DHLApiError(f"Mój DHL recover failed: HTTP {status}")
        self._adopt_access_token(token)
        return token

    async def async_refresh(self, device_id: str) -> str:
        """Mint a fresh bearer token from the stored cookie jar before a poll."""
        status, response = await self._json("GET", "/auth/refresh", params={"deviceId": device_id, "deviceName": DEVICE_NAME})
        if status in (401, 403):
            return await self._async_recover(device_id)
        token = response.get("token") if isinstance(response, dict) else None
        if status != 200 or not isinstance(token, str):
            raise DHLApiError(f"Mój DHL refresh failed: HTTP {status}")
        self._adopt_access_token(token)
        return token
