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


class DHLPaketUnreachableError(DHLApiError):
    """The endpoint did not answer like itself, most likely a non-German IP.

    Outside Germany it redirects or stalls for every code alike, so the
    first one ends the whole batch rather than each code waiting its turn.
    """


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
            if 300 <= resp.status < 400:
                raise DHLPaketUnreachableError(f"HTTP {resp.status}")
            if resp.status != 200:
                raise DHLApiError(f"HTTP {resp.status}")
            try:
                data = await resp.json(content_type=None)
            except ValueError:
                data = None
    except aiohttp.ClientError as err:
        raise DHLPaketUnreachableError(str(err)) from err
    except TimeoutError as err:
        raise DHLPaketUnreachableError("timed out") from err
    if not isinstance(data, dict):
        raise DHLPaketUnreachableError("unexpected answer (not a JSON object)")
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

    One code failing on its own does not cost the others their answer; only
    when every request failed is that raised. A
    :class:`DHLPaketUnreachableError` cancels the rest of the batch.
    """
    codes = [c for c in codes if paket_shaped(c)]
    if not codes:
        return {}
    limit = asyncio.Semaphore(DHL_PAKET_MAX_PARALLEL_REQUESTS)
    results: dict[str, dict] = {}
    errors: list[DHLApiError] = []

    async def fetch(code: str) -> None:
        async with limit:
            try:
                answer = await _fetch_one(session, code)
            except DHLPaketUnreachableError:
                raise
            except DHLApiError as err:
                errors.append(err)
                return
        if answer is not None:
            results[code] = answer

    try:
        async with asyncio.TaskGroup() as group:
            for code in codes:
                group.create_task(fetch(code))
    except ExceptionGroup as failed:
        raise failed.exceptions[0] from None
    if errors and len(errors) == len(codes):
        raise errors[0]
    for error in errors:
        _LOGGER.debug("DHL Paket request failed for one code: %s", error)
    return results
