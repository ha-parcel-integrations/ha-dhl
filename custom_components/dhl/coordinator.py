"""Coordinator for the DHL parcel tracker integration.

Fetching and event firing only — the parcel mapping lives in :mod:`.parcels`
(which dispatches into :mod:`.countries.de`). One endpoint serves both
models (BUILD_PLAN.md §4): the account inbox is fetched unconditionally,
and any manually-tracked code (`dhl.track_parcel`) not already present in the
inbox is fetched by number and merged in.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import DHLApiClient, DHLApiError, DHLAuthError
from .const import (
    CONF_INCLUDE_HISTORY,
    CONF_REFRESH_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_TRACKED_CODES,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    NEW_ISSUE_URL,
    ParcelStatus,
)
from .countries.de.session import DHLDeSession
from .parcels import apply_delivered_filter, normalize_parcel, sort_parcels_by_ts

_LOGGER = logging.getLogger(__name__)


def _refresh_interval(entry: ConfigEntry) -> timedelta:
    """Return the configured refresh interval as a ``timedelta``."""
    minutes = int(entry.options.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL))
    return timedelta(minutes=minutes)


class DHLCoordinator(DataUpdateCoordinator[list[dict]]):
    """Polls the account inbox (+ manually-tracked codes) and publishes the canonical lists.

    ``coordinator.data`` is the active (not-yet-delivered) parcels,
    ``self.delivered`` the rest.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: DHLApiClient,
        entry: ConfigEntry,
        *,
        de_session: DHLDeSession,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Passing config_entry makes self.config_entry available on the
            # base class, which every helper below relies on.
            config_entry=entry,
            name=DOMAIN,
            update_interval=_refresh_interval(entry),
        )
        self._client = client
        self._de_session = de_session
        self.delivered: list[dict] = []
        # barcode -> last seen ParcelStatus / (planned_from, planned_to).
        # ``None`` on the first refresh so events are suppressed for parcels
        # that already existed when the integration started — otherwise every
        # restart would flood users with "registered" notifications.
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: (
            dict[str, tuple[str | None, str | None]] | None
        ) = None
        # Cached device id, attached to every fired event so device-trigger
        # automations can filter to this account's device.
        self._cached_device_id: str | None = None
        # Timestamp of the last successful poll (diagnostic sensor).
        self.last_success_time: datetime | None = None
        # rateLimited is in-band on every response (BUILD_PLAN.md §5) — warn
        # only on the false->true flip, not on every poll it stays true.
        self._rate_limited_last = False
        # Consecutive-poll streak at OUT_FOR_DELIVERY per barcode, and which
        # barcodes have already been warned about — the suspected
        # Packstation-arrival case the fortschritt ladder can't express
        # (BUILD_PLAN.md §6/§7a).
        self._out_for_delivery_streak: dict[str, int] = {}
        self._stall_warned: set[str] = set()

    def _device_id(self) -> str | None:
        """Resolve (and cache) this entry's device id for event payloads."""
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(
                dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)
            ),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    @property
    def _include_history(self) -> bool:
        """Whether the opt-in per-parcel history option is enabled."""
        return bool(
            self.config_entry.options.get(
                CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
            )
        )

    def _tracked_codes(self) -> list[str]:
        """Return the manually-tracked piece codes (`dhl.track_parcel`)."""
        return list(self.config_entry.options.get(CONF_TRACKED_CODES, []))

    async def _async_fetch_missing_tracked(self, missing: list[str]) -> list[dict]:
        """Fetch every tracked code not already present in the inbox.

        A single tracked code's failure is logged and skipped rather than
        failing the whole poll — the inbox fetch already succeeded, so one
        bad by-number lookup should not take the rest down with it. An
        expired session is the exception: it is not one code's problem, so
        it is re-raised for the caller to turn into a reauth.
        """
        if not missing:
            return []
        results = await asyncio.gather(
            *(self._client.async_get_by_number(code) for code in missing),
            return_exceptions=True,
        )
        extra: list[dict] = []
        for code, result in zip(missing, results):
            if isinstance(result, DHLAuthError):
                raise result
            if isinstance(result, (DHLApiError, aiohttp.ClientError)):
                _LOGGER.warning("DHL fetch failed for tracked code %s: %s", code, result)
                continue
            if isinstance(result, BaseException):
                raise result
            if result is not None:
                extra.append(result)
        return extra

    def _warn_rate_limited(self, rate_limited: bool) -> None:
        """Log a WARNING on the false->true flip only (BUILD_PLAN.md §5)."""
        if rate_limited and not self._rate_limited_last:
            _LOGGER.warning(
                "DHL Germany reported rateLimited=true — the integration "
                "will keep polling at the configured interval; consider "
                "lengthening it if this repeats."
            )
        self._rate_limited_last = rate_limited

    def _track_out_for_delivery_stall(self, parcels: list[dict]) -> None:
        """Warn once per barcode stuck at OUT_FOR_DELIVERY across polls.

        The fortschritt ladder cannot express `at_pickup_point` at all
        (BUILD_PLAN.md §6) — a parcel that arrived at a Packstation is the
        most likely thing hiding behind a status that never advances past
        "out for delivery".
        """
        seen = set()
        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode:
                continue
            if parcel["status"] != ParcelStatus.OUT_FOR_DELIVERY:
                continue
            seen.add(barcode)
            streak = self._out_for_delivery_streak.get(barcode, 0) + 1
            self._out_for_delivery_streak[barcode] = streak
            if streak > 1 and barcode not in self._stall_warned:
                self._stall_warned.add(barcode)
                _LOGGER.warning(
                    "DHL parcel %s has stayed 'out for delivery' across "
                    "more than one poll — this may be a Packstation/Filiale "
                    "arrival the status ladder cannot express. Open an "
                    "issue: %s",
                    barcode,
                    NEW_ISSUE_URL,
                )
        for barcode in list(self._out_for_delivery_streak):
            if barcode not in seen:
                self._out_for_delivery_streak.pop(barcode, None)
                self._stall_warned.discard(barcode)

    def _persist_refresh_token_if_rotated(self) -> None:
        """Persist a rotated refresh token, if the last refresh changed it."""
        if not self._de_session.pop_refresh_token_changed():
            return
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                CONF_REFRESH_TOKEN: self._de_session.refresh_token,
            },
        )

    async def _async_update_data(self) -> list[dict]:
        """Fetch the account inbox (+ tracked codes) and split active vs delivered.

        ``aiohttp.ClientError`` and ``DHLApiError`` are deliberately
        not caught — ``DataUpdateCoordinator`` turns them into ``UpdateFailed``
        with backoff. Only an expired session needs special handling, because
        retrying it forever would never recover.
        """
        try:
            elements, rate_limited = await self._client.async_get_incoming()
        except DHLAuthError as err:
            raise ConfigEntryAuthFailed("DHL session expired") from err

        self._warn_rate_limited(rate_limited)

        inbox_barcodes = {element.get("id") for element in elements if element.get("id")}
        missing = [
            code for code in self._tracked_codes() if code not in inbox_barcodes
        ]
        try:
            elements.extend(await self._async_fetch_missing_tracked(missing))
        except DHLAuthError as err:
            raise ConfigEntryAuthFailed("DHL session expired") from err

        include_history = self._include_history
        normalized = [
            normalize_parcel(raw, country="DE", include_history=include_history)
            for raw in elements
        ]
        active = [parcel for parcel in normalized if not parcel["delivered"]]
        delivered = [parcel for parcel in normalized if parcel["delivered"]]

        self.delivered = apply_delivered_filter(
            sort_parcels_by_ts(delivered, "delivered_at", descending=True),
            self.config_entry,
        )
        normalized_active = sort_parcels_by_ts(active, "planned_from")

        # Incoming = active + delivered, combined so the transition to
        # delivered is visible in one set.
        incoming = normalized_active + self.delivered
        self._track_out_for_delivery_stall(incoming)
        self._fire_change_events(incoming)
        self._known_state = {
            parcel["barcode"]: parcel["status"]
            for parcel in incoming
            if parcel.get("barcode")
        }
        self._known_delivery_times = {
            parcel["barcode"]: (parcel.get("planned_from"), parcel.get("planned_to"))
            for parcel in incoming
            if parcel.get("barcode")
        }

        self._persist_refresh_token_if_rotated()
        self.last_success_time = datetime.now(timezone.utc)
        return normalized_active

    def _fire_change_events(self, parcels: list[dict]) -> None:
        """Fire registered / status-changed / delivered / delivery-time events.

        Silent on the very first refresh — we cannot know which parcels are
        genuinely new versus already present before HA started.

        The event contract, identical across the suite:

        * every payload is the full normalised parcel plus ``device_id``;
        * the hop **to** ``delivered`` fires only ``_parcel_delivered``, never
          also ``_parcel_status_changed``;
        * a barcode first seen already-delivered fires nothing;
        * ``registered`` only fires for a new, not-yet-delivered barcode;
        * an ETA going ``value → null`` is intentionally silent — the carrier
          just lost the window, which is not worth waking someone up for.
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
