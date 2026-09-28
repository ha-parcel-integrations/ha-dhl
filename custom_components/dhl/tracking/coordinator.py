"""Coordinator for the tracking-code source.

Every poll batches the whole gateway-routed subset in one cheap, unthrottled
request. The Express-routed subset shares ha-ups's throttled-queue model
instead — one token bucket per entry, spent on at most one Express code per
cycle, ranked by :data:`_QUEUE_PRIORITY` — because the Express backend's
 answers a handful of requests and then stands down with no
advance warning (see ``express.py`` and the research this was sized from).

A 10-character code Express cannot answer is tried once against DHL Freight
Sweden's backend (``hamta.py``); a code found there is polled there from then
on, in one batch per cycle, and never queued for Express again.
"""
from __future__ import annotations

import hashlib
import logging
import random
import time
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from ..account.coordinator import compute_poll_interval
from ..const import (
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DEFAULT_INCLUDE_HISTORY,
    DHL_EXPRESS_JITTER_FRACTION,
    DHL_EXPRESS_MIN_CYCLE_GAP_SECONDS,
    DHL_EXPRESS_REQUEST_BUDGET_CAPACITY,
    DHL_EXPRESS_REQUEST_BUDGET_REFILL_SECONDS,
    DHL_EXPRESS_STAGGER_MINUTES,
    DHL_EXPRESS_TRACKED_CODE_SOFT_LIMIT,
    DIRECTION_OUTGOING,
    DOMAIN,
    TRACKING_STORAGE_KEY,
    TRACKING_STORAGE_VERSION,
    DHLApiError,
    DHLExpressCredentialError,
    DHLExpressThrottledError,
    ParcelStatus,
)
from ..delivery_window import end_of_day
from . import (
    BACKEND_EXPRESS,
    BACKEND_GATEWAY,
    BACKEND_UNKNOWN,
    classify_shape,
    tracked_direction,
)
from .budget import RequestBudget
from .express import async_fetch_express
from .gateway import DHLGatewayError, async_fetch_gateway
from .hamta import async_fetch_hamta, hamta_shaped
from .parcels import (
    BACKEND_KEY,
    apply_delivered_filter,
    normalize_parcel,
    sort_parcels_by_ts,
    tracking_page_locale,
    tracking_page_url,
)

_LOGGER = logging.getLogger(__name__)

# Same shape as ha-ups's queue: never-attempted codes always come first, then
# whatever can plausibly change soonest. A parcel waiting at a pickup point
# cannot change until a human collects it, so it sorts last.
_QUEUE_PRIORITY = (
    ParcelStatus.OUT_FOR_DELIVERY,
    ParcelStatus.PROBLEM,
    ParcelStatus.RETURNING,
    ParcelStatus.IN_TRANSIT,
    ParcelStatus.REGISTERED,
    ParcelStatus.UNKNOWN,
    ParcelStatus.AT_PICKUP_POINT,
)

# See ha-ups's own const.py for the identical reasoning: the floor has to
# clear a full rotation of the queue, so it is sized from the queue length,
# not fixed.
_MIN_OVERDUE_INTERVALS = 3

# Backoff on a DRG10012 stand-down: `BACKOFF_BASE_SECONDS * 2**failures`,
# capped, and never shorter than a refill interval (enforced below) — a
# shorter stand-down would poll straight back into the cooldown it exists to
# wait out. The base is the refill interval itself, so the first stand-down
# already sits at "one full refill, doubled" rather than "one refill".
_BACKOFF_BASE_SECONDS = DHL_EXPRESS_REQUEST_BUDGET_REFILL_SECONDS
_BACKOFF_CAP_SECONDS = DHL_EXPRESS_REQUEST_BUDGET_REFILL_SECONDS * 6

STORE_SAVE_DELAY_SECONDS = 15


def _stagger_minutes(entry_id: str) -> int:
    """Deterministic per-install offset, stable across restarts."""
    # Not hash(): str hashes are salted per process, so the offset moved on
    # every restart.
    digest = hashlib.sha256(entry_id.encode()).hexdigest()
    return int(digest, 16) % DHL_EXPRESS_STAGGER_MINUTES


def _placeholder_raw(code: str, backend: str) -> dict:
    """Build a minimal raw payload so an unfetched code still gets a sensor."""
    if backend == BACKEND_EXPRESS:
        return {"id": code, "status": "", "checkpoints": [], BACKEND_KEY: "express"}
    return {"barcode": code, "events": [], BACKEND_KEY: "gateway"}


class DHLTrackingCoordinator(DataUpdateCoordinator[list[dict]]):
    """Polls every tracked code and publishes the canonical parcel lists.

    ``coordinator.data`` is the active (not-yet-delivered) parcels,
    ``self.delivered`` the rest, and ``outgoing``/``delivered_outgoing`` the
    same split for codes the user filed as outgoing — same contract as the
    account coordinator. ``de_session`` and ``last_element_count`` are always
    ``None`` purely so the platform files and diagnostics can treat both
    coordinators interchangeably without a source-specific branch.
    """

    def __init__(self, hass: HomeAssistant, client, entry: ConfigEntry) -> None:
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
        # Always None — see the class docstring.
        self.de_session = None
        self.last_element_count: int | None = None

        # code -> last successful raw payload (tagged with its backend), so a
        # cycle that could not afford (or chose not) to fetch a code still
        # republishes its last known data instead of dropping the sensor.
        self._raw_cache: dict[str, dict] = {}
        self._delivered_codes: set[str] = set()
        # Express-queue bookkeeping only — the gateway is never rationed.
        self._attempted_codes: set[str] = set()
        self._last_fetch_by_code: dict[str, float] = {}
        self._status_by_code: dict[str, ParcelStatus] = {}
        self._store: Store = Store(
            hass, TRACKING_STORAGE_VERSION, f"{TRACKING_STORAGE_KEY}.{entry.entry_id}"
        )
        # Restored from disk in async_load_cache.
        self._budget = RequestBudget(
            capacity=DHL_EXPRESS_REQUEST_BUDGET_CAPACITY,
            refill_seconds=DHL_EXPRESS_REQUEST_BUDGET_REFILL_SECONDS,
        )
        self._consecutive_failures = 0
        self._standdown_until_utc: float | None = None
        # A 401/403 means the credential itself was rejected — not
        # recoverable by retrying, so Express fetching stops for the rest of
        # this running entry rather than repeating the failure every cycle.
        self._express_disabled = False
        self._warned_soft_limit = False
        # Codes Hamta already answered not-found while Express was out of
        # action, so a stand-down does not re-ask Hamta every cycle.
        self._hamta_misses: set[str] = set()

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
    def express_budget_available(self) -> int:
        """Whole Express request tokens spendable right now (diagnostics)."""
        return self._budget.available()

    @property
    def express_consecutive_failures(self) -> int:
        """Consecutive DRG10012 stand-downs behind the current backoff (diagnostics)."""
        return self._consecutive_failures

    @property
    def express_standing_down(self) -> bool:
        """Whether a DRG10012 stand-down is currently in effect (diagnostics)."""
        return self._standing_down()

    @property
    def express_disabled(self) -> bool:
        """Whether Express polling was permanently aborted this run (diagnostics)."""
        return self._express_disabled

    async def async_load_cache(self) -> None:
        """Restore the payload cache, Express budget and stand-down from disk.

        Called once during setup, before the first refresh, so Express parcels
        show their last known data instead of placeholders and a restart
        during a stand-down keeps waiting it out. A missing or malformed store
        is not an error — the coordinator simply starts cold.
        """
        stored = await self._store.async_load()
        if not isinstance(stored, dict):
            return
        raw_cache = stored.get("raw_cache")
        if isinstance(raw_cache, dict):
            self._raw_cache = {
                code: raw
                for code, raw in raw_cache.items()
                if isinstance(code, str)
                and isinstance(raw, dict)
                and raw.get(BACKEND_KEY) in ("gateway", "express", "hamta")
            }
        delivered_codes = stored.get("delivered_codes")
        if isinstance(delivered_codes, list):
            self._delivered_codes = {c for c in delivered_codes if isinstance(c, str)}
        attempted_codes = stored.get("attempted_codes")
        if isinstance(attempted_codes, list):
            self._attempted_codes = {c for c in attempted_codes if isinstance(c, str)}
        per_code = stored.get("last_fetch_by_code")
        if isinstance(per_code, dict):
            self._last_fetch_by_code = {
                code: float(when)
                for code, when in per_code.items()
                if isinstance(code, str) and isinstance(when, (int, float))
            }
        failures = stored.get("consecutive_failures")
        if isinstance(failures, int) and failures > 0:
            self._consecutive_failures = failures
        standdown_until = stored.get("standdown_until_utc")
        if isinstance(standdown_until, (int, float)):
            # A deadline, not a duration: re-deriving it from the failure
            # count would restart the stand-down at full length on every boot.
            self._standdown_until_utc = float(standdown_until)
        self._budget = RequestBudget.from_dict(
            stored.get("budget"),
            DHL_EXPRESS_REQUEST_BUDGET_CAPACITY,
            DHL_EXPRESS_REQUEST_BUDGET_REFILL_SECONDS,
        )
        _LOGGER.debug(
            "DHL tracking restored cache: %s payloads, %s attempted Express "
            "codes, %s delivered, %s consecutive stand-downs, %s Express "
            "request(s) of budget left",
            len(self._raw_cache),
            len(self._attempted_codes),
            len(self._delivered_codes),
            self._consecutive_failures,
            self._budget.available(),
        )

    def _persist_cache(self) -> None:
        """Queue a debounced write of the cache and the Express queue state."""
        self._store.async_delay_save(
            lambda: {
                "raw_cache": self._raw_cache,
                "delivered_codes": sorted(self._delivered_codes),
                "attempted_codes": sorted(self._attempted_codes),
                "last_fetch_by_code": self._last_fetch_by_code,
                "consecutive_failures": self._consecutive_failures,
                "standdown_until_utc": self._standdown_until_utc,
                "budget": self._budget.as_dict(),
            },
            STORE_SAVE_DELAY_SECONDS,
        )

    async def async_remove_cache(self) -> None:
        """Delete this entry's persisted cache."""
        await self._store.async_remove()

    @property
    def _include_history(self) -> bool:
        return bool(
            self.config_entry.options.get(
                CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
            )
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
            iter(
                dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)
            ),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    def _standing_down(self) -> bool:
        if not self._standdown_until_utc:
            return False
        return self._standdown_until_utc - time.time() > 0

    def _register_express_throttle(self) -> None:
        self._consecutive_failures += 1
        backoff = min(
            _BACKOFF_BASE_SECONDS * 2**self._consecutive_failures,
            _BACKOFF_CAP_SECONDS,
        )
        # Never shorter than the configured floor: see the module
        # docstring's reasoning. Stagger (deterministic per install) plus a
        # fresh per-cycle jitter spread multiple installs' recovery apart,
        # same as ha-ups's own backoff scheduling.
        backoff = max(backoff, DHL_EXPRESS_MIN_CYCLE_GAP_SECONDS)
        stagger = _stagger_minutes(self.config_entry.entry_id) * 60
        jitter = random.uniform(0, backoff * DHL_EXPRESS_JITTER_FRACTION)
        total = backoff + stagger + jitter
        self._standdown_until_utc = time.time() + total
        _LOGGER.warning(
            "DHL Express's abuse heuristic triggered — standing down for "
            "%.0f minutes. This is expected under normal use and needs no "
            "action; the Express half of tracking resumes automatically.",
            total / 60,
        )

    def _express_queue(self, candidates: list[str]) -> list[str]:
        """Rank Express-routed candidates for the next request.

        Ported from ha-ups's ``_fetch_queue`` — see that module for the full
        reasoning on the overdue band. Delivered codes are filtered out by
        the caller before this is reached.
        """
        ranking = {status: rank for rank, status in enumerate(_QUEUE_PRIORITY)}
        unknown_rank = len(_QUEUE_PRIORITY)
        now = time.time()
        max_overdue = max(_MIN_OVERDUE_INTERVALS, len(candidates) + 1)

        def sort_key(code: str) -> tuple[int, int, int, float]:
            never_attempted = code not in self._attempted_codes
            status = self._status_by_code.get(code)
            last_fetch = self._last_fetch_by_code.get(code, 0.0)
            overdue = min(
                int((now - last_fetch) // DHL_EXPRESS_REQUEST_BUDGET_REFILL_SECONDS),
                max_overdue,
            )
            return (
                0 if never_attempted else 1,
                -overdue,
                ranking.get(status, unknown_rank),
                last_fetch,
            )

        return sorted(candidates, key=sort_key)

    def _record_express_attempt(self, code: str) -> None:
        self._budget.try_spend()
        self._attempted_codes.add(code)
        self._last_fetch_by_code[code] = time.time()

    def _backend_of(self, code: str) -> str | None:
        cached = self._raw_cache.get(code)
        return cached.get(BACKEND_KEY) if cached else None

    async def _async_try_hamta(self, code: str) -> None:
        """Try a code Express could not answer against DHL Freight Sweden."""
        if not hamta_shaped(code) or self._backend_of(code) == "express":
            return
        try:
            results = await async_fetch_hamta(self._client, [code])
        except DHLApiError as err:
            _LOGGER.warning("DHL Freight fetch failed for %s: %s", code, err)
            return
        if code in results:
            self._raw_cache[code] = {**results[code], BACKEND_KEY: "hamta"}
            self._hamta_misses.discard(code)
        else:
            self._hamta_misses.add(code)

    async def _async_fetch_one_express(self, code: str) -> None:
        """Spend the cycle's one Express request, if any, on ``code``.

        Whenever that leaves the code without an Express record, it falls
        through to Hamta.
        """
        await self._async_fetch_express_request(code)
        await self._async_try_hamta(code)

    async def _async_fetch_express_request(self, code: str) -> None:
        try:
            payload = await async_fetch_express(self._client, code)
        except DHLExpressThrottledError:
            self._register_express_throttle()
            return
        except DHLExpressCredentialError as err:
            self._express_disabled = True
            _LOGGER.warning(
                "DHL Express rejected the credential (%s) — Express "
                "tracking is disabled until this integration ships an "
                "updated one. Existing Express parcels keep showing their "
                "last known data.",
                err,
            )
            return
        except DHLApiError as err:
            _LOGGER.warning("DHL Express fetch failed for %s: %s", code, err)
            # A failed request still reached the backend and counts towards
            # its throttle. Without spending, the code stayed first in the
            # queue and was retried on every poll, outside the budget.
            self._record_express_attempt(code)
            return

        self._record_express_attempt(code)
        self._consecutive_failures = 0
        self._standdown_until_utc = None
        if payload is not None:
            self._raw_cache[code] = {**payload, BACKEND_KEY: "express"}

    async def _async_update_data(self) -> list[dict]:
        codes = self._tracked_codes()
        tracked = set(codes)
        self._raw_cache = {c: r for c, r in self._raw_cache.items() if c in tracked}
        self._delivered_codes &= tracked
        self._attempted_codes &= tracked
        self._hamta_misses &= tracked
        self._status_by_code = {
            c: s for c, s in self._status_by_code.items() if c in tracked
        }
        self._last_fetch_by_code = {
            c: t for c, t in self._last_fetch_by_code.items() if c in tracked
        }

        shapes = {code: classify_shape(code) for code in codes}
        gateway_codes = [c for c in codes if shapes[c] == BACKEND_GATEWAY]
        express_codes = [c for c in codes if shapes[c] == BACKEND_EXPRESS]
        unknown_codes = [c for c in codes if shapes[c] == BACKEND_UNKNOWN]

        # Every gateway-shaped and unknown-shaped code goes out in one batch —
        # never a confidently Express-shaped one, which the gateway has never
        # been observed to resolve.
        gateway_query = gateway_codes + unknown_codes
        gateway_results: dict[str, dict] = {}
        if gateway_query:
            try:
                gateway_results = await async_fetch_gateway(self._client, gateway_query)
            except DHLGatewayError as err:
                _LOGGER.warning("DHL tracking gateway request failed: %s", err)
        for code, raw in gateway_results.items():
            self._raw_cache[code] = {**raw, BACKEND_KEY: "gateway"}

        # The plan's one narrow, deliberately-inferred rule: a code that
        # matched no known barcode family and the gateway could not resolve
        # gets one attempt against the Express backend too, sharing its
        # queue with the confidently Express-shaped codes.
        unresolved_unknown = [c for c in unknown_codes if c not in gateway_results]
        hamta_codes = [
            c for c in express_codes + unresolved_unknown if self._backend_of(c) == "hamta"
        ]
        hamta_active = [c for c in hamta_codes if c not in self._delivered_codes]
        if hamta_active:
            try:
                hamta_results = await async_fetch_hamta(self._client, hamta_active)
            except DHLApiError as err:
                _LOGGER.warning("DHL Freight request failed: %s", err)
                hamta_results = {}
            for code, raw in hamta_results.items():
                self._raw_cache[code] = {**raw, BACKEND_KEY: "hamta"}

        express_candidates = [
            c
            for c in express_codes + unresolved_unknown
            if c not in self._delivered_codes and c not in hamta_codes
        ]

        if len(express_candidates) > DHL_EXPRESS_TRACKED_CODE_SOFT_LIMIT:
            if not self._warned_soft_limit:
                self._warned_soft_limit = True
                _LOGGER.warning(
                    "DHL is tracking %s Express-routed parcel(s), and this "
                    "backend allows roughly one request per %.0f minutes. "
                    "Each will only refresh about every %.0f hours until "
                    "some are delivered or removed.",
                    len(express_candidates),
                    DHL_EXPRESS_REQUEST_BUDGET_REFILL_SECONDS / 60,
                    len(express_candidates)
                    * DHL_EXPRESS_REQUEST_BUDGET_REFILL_SECONDS
                    / 3600,
                )
        else:
            self._warned_soft_limit = False

        if (
            express_candidates
            and not self._express_disabled
            and not self._standing_down()
            and self._budget.available() >= 1
        ):
            queue = self._express_queue(express_candidates)
            await self._async_fetch_one_express(queue[0])
        elif self._express_disabled or self._standing_down():
            # Express is out of action, so it cannot tell a Freight code
            # apart; give Hamta one code that has no record yet.
            untried = [
                c
                for c in self._express_queue(express_candidates)
                if c not in self._raw_cache and c not in self._hamta_misses
            ]
            if untried:
                await self._async_try_hamta(untried[0])

        raws: list[tuple[str, dict]] = []
        for code in codes:
            cached = self._raw_cache.get(code)
            if cached is not None:
                raws.append((code, cached))
            else:
                raws.append((code, _placeholder_raw(code, shapes[code])))

        include_history = self._include_history
        normalized = [
            normalize_parcel(raw, include_history=include_history) for _, raw in raws
        ]
        locale = tracking_page_locale(self.hass)
        for (code, _), parcel in zip(raws, normalized):
            if not parcel.get("barcode"):
                parcel["barcode"] = code
            parcel["url"] = tracking_page_url(code, locale)
            if parcel["planned_from"] and not parcel["planned_to"]:
                # A single moment from DHL means "that day", not "that second".
                parcel["planned_to"] = end_of_day(parcel["planned_from"])
            if code not in self._raw_cache:
                # Nothing from DHL yet; the placeholder is not a DHL record.
                parcel["raw"] = {}

        self._delivered_codes = {
            code for (code, _), parcel in zip(raws, normalized) if parcel["delivered"]
        }
        self._status_by_code = {
            code: parcel["status"] for (code, _), parcel in zip(raws, normalized)
        }

        outgoing_codes = {
            item[CONF_TRACKING_CODE]
            for item in self.config_entry.options.get(CONF_PARCELS, [])
            if item.get(CONF_TRACKING_CODE)
            and tracked_direction(item) == DIRECTION_OUTGOING
        }
        received = [
            parcel
            for (code, _), parcel in zip(raws, normalized)
            if code not in outgoing_codes
        ]
        sent = [
            parcel
            for (code, _), parcel in zip(raws, normalized)
            if code in outgoing_codes
        ]

        active = [p for p in received if not p["delivered"]]
        delivered = [p for p in received if p["delivered"]]

        self.delivered = apply_delivered_filter(
            sort_parcels_by_ts(delivered, "delivered_at", descending=True),
            self.config_entry,
        )
        normalized_active = sort_parcels_by_ts(active, "planned_from")

        incoming = normalized_active + self.delivered
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
            parcel["barcode"]: (parcel["status"], parcel["delivered"])
            for parcel in outgoing
            if parcel.get("barcode")
        }

        self.last_success_time = datetime.now(timezone.utc)
        self._schedule(normalized_active + self.outgoing)
        self._persist_cache()
        return normalized_active

    def _schedule(self, active_parcels: list[dict]) -> None:
        """Set ``update_interval``, letting an Express stand-down outrank it.

        The gateway is unthrottled and drives the normal cadence
        (``compute_poll_interval``, reused from the account source); the
        Express budget only ever gates *whether* this cycle spent its one
        Express request, never the interval — except when a stand-down is
        active and longer than the normal cadence would wait anyway, the
        same override ha-ups's ``_schedule`` applies.
        """
        base = compute_poll_interval(self.config_entry.entry_id, active_parcels)
        standdown_left = (
            self._standdown_until_utc - time.time() if self._standdown_until_utc else 0.0
        )
        if standdown_left > base.total_seconds():
            self.update_interval = timedelta(seconds=standdown_left)
        else:
            self.update_interval = base

    def _fire_outgoing_change_events(self, parcels: list[dict]) -> None:
        """Fire status/delivered events for outgoing parcels.

        Identical contract to the account coordinator's outgoing events.
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
