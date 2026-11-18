"""DHL Paket Germany's public by-number lookup (dhl.de): fetch only.

The answer is the same ``sendungen`` element the account source reads, so it
is normalized by the account's ``normalize_parcel_de``.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from ..account.countries.de import is_not_found
from ..const import (
    DHL_DE_TRACKING_HEADERS,
    DHL_DE_TRACKING_URL,
    DHL_PAKET_MAX_PARALLEL_REQUESTS,
    DHL_PAKET_MIN_CODE_LENGTH,
    DHL_PAKET_REQUEST_TIMEOUT_SECONDS,
    DHLApiError,
)

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=DHL_PAKET_REQUEST_TIMEOUT_SECONDS)


def paket_shaped(code: str) -> bool:
    """Whether ``code`` can be a DHL Paket piece code at all."""
    return len(code) >= DHL_PAKET_MIN_CODE_LENGTH and code.isalnum()


async def _fetch_one(session: aiohttp.ClientSession, code: str) -> dict | None:
    try:
        async with session.get(
            DHL_DE_TRACKING_URL,
            params={
                "piececode": code,
                "noRedirect": "true",
                "language": "de",
                "cid": "app",
            },
            headers=DHL_DE_TRACKING_HEADERS,
            timeout=_TIMEOUT,
            # Outside Germany the endpoint redirects to an error page.
            allow_redirects=False,
        ) as resp:
            if resp.status != 200:
                raise DHLApiError(f"HTTP {resp.status}")
            try:
                data = await resp.json(content_type=None)
            except ValueError:
                data = None
    except aiohttp.ClientError as err:
        raise DHLApiError(str(err)) from err
    except TimeoutError as err:
        raise DHLApiError("timed out") from err
    if not isinstance(data, dict):
        raise DHLApiError("unexpected answer (not a JSON object)")
    if data.get("rateLimited"):
        raise DHLApiError("rate limited")
    # The answer can carry other shipments the same visitor looked up before,
    # so only the element for the code asked counts.
    for element in data.get("sendungen") or []:
        if isinstance(element, dict) and str(element.get("id") or "").upper() == code:
            return None if is_not_found(element) else element
    return None


async def async_fetch_paket(
    session: aiohttp.ClientSession, codes: list[str]
) -> dict[str, dict]:
    """Fetch codes in parallel; return ``{code: element}`` for the ones DHL Paket knows.

    One failing code does not cost the others their answer; only when every
    request failed is that raised.
    """
    codes = [c for c in codes if paket_shaped(c)]
    if not codes:
        return {}
    limit = asyncio.Semaphore(DHL_PAKET_MAX_PARALLEL_REQUESTS)

    async def fetch(code: str) -> dict | None:
        async with limit:
            return await _fetch_one(session, code)

    answers = await asyncio.gather(*(fetch(c) for c in codes), return_exceptions=True)
    errors = [a for a in answers if isinstance(a, BaseException)]
    for error in errors:
        if not isinstance(error, DHLApiError):
            raise error
    if errors and len(errors) == len(codes):
        raise errors[0]
    for error in errors:
        _LOGGER.debug("DHL Paket request failed for one code: %s", error)
    return {
        code: answer
        for code, answer in zip(codes, answers, strict=True)
        if isinstance(answer, dict)
    }
