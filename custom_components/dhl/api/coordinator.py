"""Coordinator for the API source: every tracked code, one Unified API call each.

No routing, no budget, no persisted cache — the account-less polling model
as-is. Two things are the API's hard limits rather than a budget: requests are
sequential and at least ``DHL_UNIFIED_MIN_REQUEST_GAP_SECONDS`` apart (DHL
allows one call per 5 s per key), and a 429 stops the poll and backs off.

The key is one credential covering every code, so a 401/403 on any of them
is a reauth for the whole entry, not a per-parcel error.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ..account.coordinator import compute_poll_interval
from ..const import (
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DEFAULT_INCLUDE_HISTORY,
    DHL_UNIFIED_BACKOFF_BASE_SECONDS,
    DHL_UNIFIED_BACKOFF_CAP_SECONDS,
    DHL_UNIFIED_MAX_RETENTION_DAYS,
    DHL_UNIFIED_MIN_REQUEST_GAP_SECONDS,
    DIRECTION_OUTGOING,
    DOMAIN,
    ParcelStatus,
)
from ..tracking import tracked_direction
from ..tracking.parcels import (
    apply_delivered_filter,
    parse_iso,
    sort_parcels_by_ts,
    tracking_page_locale,
    tracking_page_url,
)
from .client import (
    DHLUnifiedClient,
    DHLUnifiedError,
    DHLUnifiedKeyError,
    DHLUnifiedNotFound,
    DHLUnifiedRateLimitError,
)
from .parcels import normalize_parcel_unified, pick_shipment

_LOGGER = logging.getLogger(__name__)


class DHLUnifiedCoordinator(DataUpdateCoordinator[list[dict]]):
    """Polls each tracked code and publishes the canonical parcel lists.

    Same list contract as the tracking coordinator: ``data`` is the active
    incoming parcels, ``delivered`` the rest, and ``outgoing``/
    ``delivered_outgoing`` the same split for codes filed as outgoing.
    ``de_session`` and ``last_element_count`` are always ``None`` so the
    platforms and diagnostics need no source-specific branch.
    """

    def __init__(
        self, hass: HomeAssistant, client: DHLUnifiedClient, entry: ConfigEntry
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=compute_poll_interval(entry.entry_id, []),
        )
        self._client = client
        self.delivered: list[dict] = []
        self.outgoing: list[dict] = []
        self.delivered_outgoing: list[dict] = []
        self.de_session = None
        self.last_element_count: int | None = None

        # In memory only: DHL's terms allow no copy of a shipment beyond 30
        # days after delivery, and this cache is the only copy there is.
        self._raw_cache: dict[str, dict] = {}
        self._delivered_codes: set[str] = set()
        # Delivered longer ago than the retention cap: never fetched or shown
        # again while the code stays tracked.
        self._expired_codes: set[str] = set()
        self._consecutive_429 = 0
        self._last_request_at: float | None = None

        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: (
            dict[str, tuple[str | None, str | None]] | None
        ) = None
        self._known_outgoing_state: dict[str, tuple[ParcelStatus, bool]] | None = (
            None
        )
        self._cached_device_id: str | None = None
        self.last_success_time: datetime | None = None

    @property
    def delivered_codes(self) -> set[str]:
        """Codes skipped from the fetch because they are delivered (diagnostics)."""
        return self._delivered_codes

    @property
    def consecutive_rate_limits(self) -> int:
        """Consecutive polls that ended on a 429 (diagnostics)."""
        return self._consecutive_429

    @property
    def _include_history(self) -> bool:
        return bool(
            self.config_entry.options.get(CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY)
        )

    def _tracked_codes(self) -> list[str]:
        return [
            item[CONF_TRACKING_CODE]
            for item in self.config_entry.options.get(CONF_PARCELS, [])
            if item.get(CONF_TRACKING_CODE)
        ]

    def _device_id(self) -> str | None:
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    async def _async_pace(self) -> None:
        """Wait until the last request to DHL is at least the minimum gap ago.

        Tracked across polls too: an options change triggers a refresh right
        after a scheduled one.
        """
        if self._last_request_at is not None:
            wait = DHL_UNIFIED_MIN_REQUEST_GAP_SECONDS - (
                time.monotonic() - self._last_request_at
            )
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_request_at = time.monotonic()

    def _expired(self, parcel: dict) -> bool:
        delivered_at = parse_iso(parcel.get("delivered_at"))
        if not parcel["delivered"] or delivered_at is None:
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(days=DHL_UNIFIED_MAX_RETENTION_DAYS)
        return delivered_at < cutoff

    async def _async_update_data(self) -> list[dict]:
        codes = self._tracked_codes()
        tracked = set(codes)
        self._raw_cache = {c: r for c, r in self._raw_cache.items() if c in tracked}
        self._delivered_codes &= tracked
        self._expired_codes &= tracked

        to_fetch = [
            c for c in codes if c not in self._delivered_codes and c not in self._expired_codes
        ]
        rate_limited: DHLUnifiedRateLimitError | None = None
        errors = 0
        for code in to_fetch:
            await self._async_pace()
            try:
                body = await self._client.async_get_shipments(code)
            except DHLUnifiedKeyError as err:
                raise ConfigEntryAuthFailed("DHL rejected the API key") from err
            except DHLUnifiedRateLimitError as err:
                rate_limited = err
                break
            except DHLUnifiedNotFound:
                continue
            except DHLUnifiedError as err:
                errors += 1
                _LOGGER.warning("DHL API fetch failed for %s: %s", code, err)
                continue
            shipment = pick_shipment(body, code)
            if shipment is not None:
                self._raw_cache[code] = shipment

        if rate_limited is not None:
            # The rest of this poll would only hit the same limit; the cache
            # keeps what the parcels last showed.
            self._consecutive_429 += 1
            retry_after = rate_limited.retry_after or min(
                DHL_UNIFIED_BACKOFF_BASE_SECONDS * 2**self._consecutive_429,
                DHL_UNIFIED_BACKOFF_CAP_SECONDS,
            )
            raise UpdateFailed("DHL API rate limit reached (429)", retry_after=retry_after)
        self._consecutive_429 = 0
        if to_fetch and errors == len(to_fetch):
            raise UpdateFailed("DHL API unreachable for every tracked parcel")

        include_history = self._include_history
        locale = tracking_page_locale(self.hass)
        entries: list[tuple[str, dict]] = []
        for code in codes:
            if code in self._expired_codes:
                continue
            cached = self._raw_cache.get(code)
            parcel = normalize_parcel_unified(
                cached or {}, code=code, include_history=include_history
            )
            parcel["url"] = tracking_page_url(code, locale)
            if cached is None:
                parcel["raw"] = {}
            if self._expired(parcel):
                self._expired_codes.add(code)
                self._raw_cache.pop(code, None)
                continue
            entries.append((code, parcel))

        self._delivered_codes = {code for code, parcel in entries if parcel["delivered"]}

        outgoing_codes = {
            item[CONF_TRACKING_CODE]
            for item in self.config_entry.options.get(CONF_PARCELS, [])
            if item.get(CONF_TRACKING_CODE)
            and tracked_direction(item) == DIRECTION_OUTGOING
        }
        received = [p for code, p in entries if code not in outgoing_codes]
        sent = [p for code, p in entries if code in outgoing_codes]

        self.delivered = apply_delivered_filter(
            sort_parcels_by_ts(
                [p for p in received if p["delivered"]], "delivered_at", descending=True
            ),
            self.config_entry,
        )
        normalized_active = sort_parcels_by_ts(
            [p for p in received if not p["delivered"]], "planned_from"
        )
        incoming = normalized_active + self.delivered
        self._fire_change_events(incoming)
        self._known_state = {p["barcode"]: p["status"] for p in incoming if p.get("barcode")}
        self._known_delivery_times = {
            p["barcode"]: (p.get("planned_from"), p.get("planned_to"))
            for p in incoming
            if p.get("barcode")
        }

        self.delivered_outgoing = apply_delivered_filter(
            sort_parcels_by_ts(
                [p for p in sent if p["delivered"]], "delivered_at", descending=True
            ),
            self.config_entry,
        )
        self.outgoing = sort_parcels_by_ts(
            [p for p in sent if not p["delivered"]], "planned_from"
        )
        outgoing = self.outgoing + self.delivered_outgoing
        self._fire_outgoing_change_events(outgoing)
        self._known_outgoing_state = {
            p["barcode"]: (p["status"], p["delivered"]) for p in outgoing if p.get("barcode")
        }

        if not to_fetch or errors < len(to_fetch):
            self.last_success_time = datetime.now(timezone.utc)

        in_flight = normalized_active + self.outgoing
        # Nothing left in flight: stop until the user adds a code, which
        # refreshes through the entry's update listener.
        self.update_interval = (
            compute_poll_interval(self.config_entry.entry_id, in_flight)
            if in_flight
            else None
        )
        return normalized_active

    def _fire_outgoing_change_events(self, parcels: list[dict]) -> None:
        """Fire status/delivered events for outgoing parcels.

        Keyed on the ``delivered`` bool, same contract as the other sources.
        """
        if self._known_outgoing_state is None:
            return
        device_id = self._device_id()
        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode or barcode not in self._known_outgoing_state:
                continue
            old_status, old_delivered = self._known_outgoing_state[barcode]
            new_status = parcel["status"]
            if parcel["delivered"] and not old_delivered:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_outgoing_parcel_delivered",
                    {**parcel, "device_id": device_id},
                )
            elif old_status != new_status:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_outgoing_parcel_status_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_status": old_status,
                        "new_status": new_status,
                    },
                )

    def _fire_change_events(self, parcels: list[dict]) -> None:
        """Fire registered / status-changed / delivered / delivery-time events.

        Identical contract to the account coordinator's — see its docstring.
        """
        if self._known_state is None:
            return
        known_times = self._known_delivery_times or {}
        device_id = self._device_id()
        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode:
                continue
            new_status = parcel["status"]
            if barcode not in self._known_state:
                if new_status != ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_registered",
                        {**parcel, "device_id": device_id},
                    )
                continue

            if self._known_state[barcode] != new_status:
                if new_status == ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_delivered",
                        {**parcel, "device_id": device_id},
                    )
                else:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_status_changed",
                        {
                            **parcel,
                            "device_id": device_id,
                            "old_status": self._known_state[barcode],
                            "new_status": new_status,
                        },
                    )

            old_from, old_to = known_times.get(barcode, (None, None))
            new_from = parcel.get("planned_from")
            new_to = parcel.get("planned_to")
            from_changed = new_from is not None and new_from != old_from
            to_changed = new_to is not None and new_to != old_to
            if from_changed or to_changed:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_parcel_delivery_time_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_planned_from": old_from,
                        "new_planned_from": new_from,
                        "old_planned_to": old_to,
                        "new_planned_to": new_to,
                    },
                )
