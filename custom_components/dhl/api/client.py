"""Transport for DHL's Unified Shipment Tracking API.

One ``GET /track/shipments`` per tracking code, authenticated by the user's
own ``DHL-API-Key`` header. The key is never logged, and neither is the full
request URL (it carries the tracking code).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import aiohttp

from ..const import (
    DHL_UNIFIED_LANGUAGE,
    DHL_UNIFIED_REQUEST_TIMEOUT_SECONDS,
    DHL_UNIFIED_URL,
    DHLApiError,
)

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=DHL_UNIFIED_REQUEST_TIMEOUT_SECONDS)


class DHLUnifiedError(DHLApiError):
    """A non-credential failure of the Unified API."""


class DHLUnifiedKeyError(DHLUnifiedError):
    """DHL rejected the API key (401/403).

    One key covers every tracked code, so this is a whole-entry condition and
    the one error here that starts Home Assistant's reauth flow.
    """


class DHLUnifiedRateLimitError(DHLUnifiedError):
    """DHL answered 429; ``retry_after`` is its own hint in seconds, if any."""

    def __init__(self, detail: str, retry_after: float | None) -> None:
        """Store the server's retry hint."""
        super().__init__(detail)
        self.retry_after = retry_after


class DHLUnifiedNotFound(DHLUnifiedError):
    """DHL has no shipment for this code (404)."""


def _retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header: delta-seconds or an HTTP date."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())


class DHLUnifiedClient:
    """Fetches one tracking code at a time with the entry's API key."""

    def __init__(self, session: aiohttp.ClientSession, api_key: str) -> None:
        """Hold the shared HA session and the user's key."""
        self._session = session
        self._api_key = api_key

    async def async_get_shipments(self, code: str) -> dict:
        """Return the response body for ``code``.

        Raises :class:`DHLUnifiedNotFound` on 404, never an empty result, so
        the caller can tell "no such shipment" from a transport failure.
        """
        try:
            async with self._session.get(
                DHL_UNIFIED_URL,
                params={"trackingNumber": code, "language": DHL_UNIFIED_LANGUAGE},
                headers={"DHL-API-Key": self._api_key, "accept": "application/json"},
                timeout=_TIMEOUT,
            ) as resp:
                _LOGGER.debug("DHL Unified API: GET /track/shipments -> HTTP %s", resp.status)
                if resp.status in (401, 403):
                    raise DHLUnifiedKeyError(f"HTTP {resp.status}")
                if resp.status == 404:
                    raise DHLUnifiedNotFound("HTTP 404")
                if resp.status == 429:
                    raise DHLUnifiedRateLimitError(
                        "HTTP 429", _retry_after(resp.headers.get("Retry-After"))
                    )
                if resp.status != 200:
                    raise DHLUnifiedError(f"HTTP {resp.status}")
                try:
                    body = await resp.json(content_type=None)
                except ValueError as err:
                    raise DHLUnifiedError("unparseable response") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise DHLUnifiedError(str(err) or type(err).__name__) from err
        if not isinstance(body, dict):
            raise DHLUnifiedError("unexpected body (not a JSON object)")
        return body
