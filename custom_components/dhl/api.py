"""Thin per-country dispatcher for the DHL transport layer.

``DHLApiClient`` is constructed once per config entry (in ``__init__.py``)
and used by the coordinator without it needing to know which country it is
talking to. All the actual HTTP mechanics live in ``countries/<code>/``; this
class only picks the right one and forwards to it.

``DHLApiError``/``DHLAuthError`` are defined in ``const.py`` (not here) so a
country module can raise them without an import cycle back through this
file — re-exported here so ``from .api import DHLApiClient, DHLApiError,
DHLAuthError`` and test patch targets
(``custom_components.dhl.api.DHLApiClient.async_get_incoming``) keep working
unchanged.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp

from .const import DEFAULT_COUNTRY, DHLApiError, DHLAuthError
from .countries.de import (
    async_get_by_number_envelope,
    async_get_inbox_envelope,
    find_element_by_id,
    is_not_found,
    needs_enrichment,
    select_active_elements,
)

if TYPE_CHECKING:
    from .countries.de.session import DHLDeSession

__all__ = ["DHLApiClient", "DHLApiError", "DHLAuthError"]

_LOGGER = logging.getLogger(__name__)


class DHLApiClient:
    """Dispatches inbox/by-number fetches to the hub's country transport.

    DE needs a :class:`~.countries.de.session.DHLDeSession` for its OIDC
    tokens — pass it as ``de_session`` when ``country="DE"``. Constructing a
    DE client without one is a configuration bug, not a runtime API failure,
    so it raises ``RuntimeError`` rather than ``DHLApiError``.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        country: str = DEFAULT_COUNTRY,
        de_session: "DHLDeSession | None" = None,
    ) -> None:
        """Initialise the client for one hub's country."""
        self._session = session
        self._country = country
        self._de_session = de_session

    def _require_de_session(self) -> "DHLDeSession":
        if self._de_session is None:
            raise RuntimeError(
                f"DHLApiClient built with country={self._country!r} but no de_session"
            )
        return self._de_session

    async def async_get_incoming(self) -> tuple[list[dict], bool]:
        """Return ``(active sendungen elements, rateLimited)`` for the account inbox.

        Archived elements are filtered (with the empties-the-list fallback
        already applied); elements that are a populated "not found" marker
        rather than a real parcel are dropped too — the inbox should never
        surface those as parcels. The inbox listing can also return a bare
        stub for a real, active shipment it hasn't detailed yet — those are
        enriched with an individual by-number fetch before the not-found
        check runs, or they would be misread as not-found instead.
        """
        if self._country != "DE":
            raise RuntimeError(f"unsupported country {self._country!r}")
        de_session = self._require_de_session()
        envelope = await async_get_inbox_envelope(self._session, de_session)
        sendungen = envelope.get("sendungen")
        elements = select_active_elements(sendungen if isinstance(sendungen, list) else [])
        elements = await self._enrich_stubs(elements)
        elements = [element for element in elements if not is_not_found(element)]
        _LOGGER.debug(
            "Account inbox: %d raw, %d active after filtering",
            len(sendungen) if isinstance(sendungen, list) else 0,
            len(elements),
        )
        return elements, bool(envelope.get("rateLimited"))

    async def _enrich_stubs(self, elements: list[dict]) -> list[dict]:
        """Replace a bare inbox stub with its by-number equivalent, where possible.

        One bad enrichment fetch falls back to the original stub rather than
        failing the whole inbox fetch — the not-found check downstream then
        drops it the same way it would a genuinely unavailable parcel.
        """
        de_session = self._require_de_session()
        stubs = [
            (index, element)
            for index, element in enumerate(elements)
            if element.get("id") and needs_enrichment(element)
        ]
        if not stubs:
            return elements

        async def _fetch(barcode: str) -> dict | None:
            envelope = await async_get_by_number_envelope(
                self._session, de_session, barcode
            )
            sendungen = envelope.get("sendungen")
            candidates = select_active_elements(
                sendungen if isinstance(sendungen, list) else []
            )
            return find_element_by_id(candidates, barcode)

        results = await asyncio.gather(
            *(_fetch(element["id"]) for _, element in stubs),
            return_exceptions=True,
        )
        enriched = list(elements)
        for (index, original), result in zip(stubs, results):
            if isinstance(result, dict):
                enriched[index] = result
        return enriched

    async def async_get_by_number(self, piece_code: str) -> dict | None:
        """Fetch one manually-tracked parcel by its piece code (`track_parcel`).

        Returns ``None`` when the number resolves to nothing, or to a
        populated "not found" marker — the caller treats both the same way
        a not-yet-scanned number would be treated.
        """
        if self._country != "DE":
            raise RuntimeError(f"unsupported country {self._country!r}")
        de_session = self._require_de_session()
        envelope = await async_get_by_number_envelope(
            self._session, de_session, piece_code
        )
        sendungen = envelope.get("sendungen")
        elements = select_active_elements(sendungen if isinstance(sendungen, list) else [])
        element = find_element_by_id(elements, piece_code)
        if element is None or is_not_found(element):
            return None
        return element
