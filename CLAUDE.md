# Working in this repository

Home Assistant custom integration for **DHL**: an `account` source (DHL Paket
Germany, DHL Parcel Polska — a logged-in inbox) and a `tracking` source (a
keyless DHL Parcel gateway plus DHL Express tracking, both code-based, no
account). Distributed via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template**. No DTO layer.

Three places hold the knowledge, and they do not overlap:

| What | Where |
|---|---|
| How this integration is built, and why it is built that way | [`ARCHITECTURE.md`](ARCHITECTURE.md) — read it before touching the OIDC session, the `fortschritt` ladder, the in/outgoing split, `account/countries/de/`, or anything in `tracking/` |
| Endpoint mechanics, status vocabularies | `carrier-research/dhl/api/` (private repo) — the OIDC discovery/token endpoints, the `int-verfolgen/data/search` envelope, the `fortschritt` ladder, the tracking gateway/Express mechanics, and every contested field. **Never** duplicated into this repo |
| Suite-wide conventions | [`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md) |

This file is the short list of things an agent must not get wrong.

## Shared conventions — fetch when relevant

Don't fetch `CONVENTIONS.md` every session — fetch it **before** you act in one
of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change the sort/first-refresh; touch unmapped-status logging | *Parcel contract* — exact key set, units, sort, events + suppression; `test_parcels.py::test_normalize_publishes_exactly_the_canonical_keys` guards the key set |
| change which optional field this carrier populates vs. always returns `None` | Update `const.py`'s `CAPABILITIES` in the same commit — it feeds the comparison table on the docs site, so an unreflected change is a wrong claim on the website |
| ship anything while below 1.0.0 (unconfirmed data) | *Pre-1.0 releases* — one-shot WARNINGs for every guessed shape/code |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client, sync requests) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Structure, options flow, dynamic polling and module layout are suite-wide**
and identical in every carrier — the authoritative spec is
[`ha-carrier-template/scaffold/CLAUDE.md`](https://github.com/ha-parcel-integrations/ha-carrier-template/blob/main/scaffold/CLAUDE.md).
Where this repo diverges from it, that is recorded below under
*Divergences from the scaffold*.

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` and half-sets-up the
  entry. Runtime-only; tests don't catch a regression.
- **Setup stale-entity sweep is scoped to `domain == "sensor"` and skips
  `non_parcel_unique_ids`** — else it deletes the refresh button / the
  summary+diagnostic sensors. Add a new non-parcel sensor's unique_id to the set.
- **Per-parcel sensors are removed by the summary sensor** via
  `entity_registry.async_remove` (self-removal races and leaves ghosts).

## Load-bearing DHL decisions — do not refactor away

**Never change the OIDC client id, redirect URI or `claims` without a live
re-test against a real account with real parcels.** Logging in with the wrong
client id *succeeds* — tokens issued, no error — but silently returns an empty
account inbox on every poll. A change that still logs in proves nothing.
`DHL_DE_LOGIN_CLAIMS` (`post_number` specifically) is part of this.

**A refreshed ID token without `post_number` must force a reauth.** `claims`
is only sent at `authorize`, never on `grant_type=refresh_token`, and a
refreshed token that comes back without the account claim still authenticates
— the inbox just answers HTTP 200 with an empty list forever. Confirmed live
on a reporter's account (issue #6): the six identity claims
(`post_number`, `email`, `display_name`, `customer_type`, `last_login`,
`service_mask`) were gone while `sub`, `sid` and `auth_time` were unchanged.
Re-authenticating is the only recovery, so `_async_refresh()` raises
`DHLDeAuthError` — after recording a rotated refresh token, never before.

**A failed enrichment must never be reported as an empty inbox.**
`needs_enrichment()` and `is_not_found()` both key off a missing
`sendungsdetails.sendungsverlauf`, so an un-enriched stub *is* "not found" by
construction. Letting a failed by-number fetch fall through to the not-found
drop makes a real parcel disappear on HTTP 200, with no warning and no reauth
prompt — two tests used to assert exactly that. `async_get_incoming()` now
raises when enrichment failed for every element.

**Requests must originate from a German IP.** The endpoint does not answer a
non-German client correctly — country-level, not bot detection. Developing from
elsewhere needs a German VPN/VM, or every request stalls until
`DHL_DE_REQUEST_TIMEOUT_SECONDS`.

**Every DE request keeps its explicit 30 s timeout.** Without one, a stalled
first refresh hangs for aiohttp's 300 s default with nothing logged (HA
suppresses first-refresh tracebacks), by which time the refresh token has
rotated server-side and the retry gets `invalid_grant`. `config_flow.py` catches
`TimeoutError` alongside `aiohttp.ClientError` — a total-timeout raises the
former.

**The stored credential is the refresh token, never a password.** One forced
refresh on a 401, **never a retry loop**. A rotated refresh token must be
persisted (`pop_refresh_token_changed` → written back to `entry.data` after
every poll) or a restart resumes with a dead token.

**`AT_PICKUP_POINT` is not in `_LADDER`; it is derived.** A Packstation arrival
is `fortschritt` 4 with `zustellung.packageStationType == "PACKAGE_STATION"` and
`abholcodeAvailable` true (issue #7, one real parcel). Filiale is **not**
mapped — no structured field was captured for it. Whether those two fields
already appear while the parcel is still on the vehicle is unverified.

**Contested fields code both branches and warn — don't collapse them to one.**
The delivered flag (`istZugestellt` else derive from the ladder, warn on
disagreement), `raw_status` (`.status`, log `.kurzStatus`), and the delivery
window (`Von`/`Bis` pair, else singular `zustellzeitfenster`/`zustelldatum`).

**Outgoing keys delivery detection differently from incoming — on purpose.**
`status` is force-`UNKNOWN` for every outgoing parcel (the ladder was calibrated
against incoming sources only), so `_fire_outgoing_change_events` detects the
terminal hop from the independently-derived `delivered` bool, not
`status == DELIVERED`. **Do not "simplify" this back to mirroring
`_fire_change_events`.**

**The outgoing direction value is `ABGEHEND`, not `AUSGEHEND`.** Both OSS
sources named `AUSGEHEND`; the first real outgoing parcel ever seen on a real
account (maintainer's own, 2026-09-19) said `ABGEHEND`, and until then every
outgoing element fell through to the incoming default. Both are recognised now.
When a vocabulary in this integration comes only from reconstruction sources,
treat it as a hypothesis — the unrecognised-value warning is what catches it.

**`sender`/`receiver` come from `sendungsinfo.sendungsname` keyed on
`sendungsrichtung`**; an unrecognised direction warns once and leaves both
`None` rather than guessing. `.zustellung.empfaenger.name` is **not** mapped to
`receiver` — it means "who physically took the parcel", not the addressee.
There is an open risk that the authenticated by-number path returns `ANKOMMEND`
unconditionally; see [`ARCHITECTURE.md`](ARCHITECTURE.md).

**Diagnostics redact leaves, never containers.** `TO_REDACT` deliberately
excludes `zustellung`/`empfaenger` — they're objects, and `async_redact_data`
replaces a redacted key's whole value, collapsing the nesting a tester's export
needs. `CONF_TRACKED_CODES` is redacted by hand (a bare string list has no key
to match).

**`weight`/`dimensions` are always `None`; `pickup_point` is set for Packstation
arrivals only** (parsed from the latest event's link text). Keep `const.py`'s
`CAPABILITIES` in sync if that changes.

**Do not design toward folding in `ha-dhl-nl`.** It is a separate released repo
on a different backend. Folding it in as `account/countries/nl/` is a later
repo-consolidation decision, and NL's auth model shares nothing with DE's — no
shared config-flow base class is worth building for two data points.

## Load-bearing PL decisions — do not refactor away

**The raw `status` (`TT_*`/`SP_*`) code is the primary status source; never
flip that order.** `_RAW` in `account/countries/pl/__init__.py` decides whenever it
knows the code. `menuTimelineLabel.status` is only the fallback for a code
that isn't in `_RAW` yet, and which vocabulary it carries is **contested**
(`carrier-research/dhl/api/dhl-pl/tracking.md`, "Contested: what
`menuTimelineLabel.status` carries"): the 9-value coarse ladder (`_LADDER`)
or the timeline names (`_TIMELINE` — `DeliveredToLocker`,
`RetrievedFromPoint`, …). The only live value seen, `Delivered`, is in both.
The fallback therefore accepts both tables; they overlap only on `Route`,
`Delivery` and `Delivered`, which map the same way. Once a real parcel settles
the field, drop the table that turned out wrong.

**The access token is split across two cookies on purpose.**
`DHLPlSession._adopt_access_token` writes a minted JWT's `header.payload` into
`access-token` and its signature into `access-signature` — exactly how DHL's
own `Set-Cookie` does it. `/auth/refresh` authenticates via this pair and never
re-issues it itself; writing the whole JWT into one cookie does not
reconstitute a valid credential, and the 30-minute session window only slides
because this method puts the minted token back into the jar every time.

**Refresh runs before every poll, not just after a failure.**
`async_get_incoming` always calls `pl_session.async_refresh()` first — the
cookie jar, not the short-lived bearer token, is the durable credential, and
persisting the rotated jar after every successful poll (`account/coordinator.py`) is
what survives a restart.

**`/auth/refresh` is useless once the access token has actually expired — that
is what `/auth/recover` is for.** Refresh authenticates *with* the access-token
cookie pair, so a token past its 30-minute life gets `401 invalid_token` and no
amount of retrying helps. Any HA downtime longer than that window therefore
used to cost the user a fresh SMS login. `_async_recover` trades the durable
`access-remember` cookie (the `rememberMe: true` artifact, verified live
2026-09-12 against a session that had been dead for 36 minutes) for a new
token, with no Altcha and no SMS. **Do not "simplify" the 401 branch of
`async_refresh` back into raising `DHLAuthError` directly** — that reintroduces
an SMS prompt after every restart that takes more than half an hour. Only an
outright `400`/`401`/`403` from recover means the credential is really gone; a
5xx stays a `DHLApiError` so an outage can't push the user into a reauth flow.

**A 401/403 from the inbox or observed-list call is a session death, not a
generic API error.** Both branches in `async_get_incoming` raise
`DHLAuthError` on 401/403 — a refreshed token can still be rejected by these
calls in ways the refresh call itself won't catch — so the coordinator starts
reauth instead of retrying a call that will never succeed.

**The config flow's throwaway session must be closed, not just discarded.**
`_get_pl_session()` opens its own `aiohttp.ClientSession` (a dedicated cookie
jar is why — DE never needs one, since it reuses HA's shared session).
`async_step_pl_sms` calls `session.aclose()` itself once the cookie jar has
been exported; `DHLConfigFlow.async_remove()` covers the abandoned-flow case
(the user quits between the phone and SMS steps). Dropping either leaves an
unclosed `ClientSession`, which `pytest_homeassistant_custom_component`'s
cleanup check turns into a flaky test failure under `--cov` — it will not
reliably reproduce without coverage instrumentation, so don't dismiss it as a
one-off if it resurfaces.

## Divergences from the scaffold

Everything not listed here follows the scaffold exactly.

*Options and reloads* — DHL is account-based (`async_schedule_reload`, no
update listener) but is also the suite's **first account-based carrier with a
`track_parcel` service**, for its by-number half. `services.py` nudges the
coordinator directly with `async_request_refresh()` after an add/remove rather
than using either stock mechanism. `options.async_init`'s form never touches
`CONF_TRACKED_CODES` — the schema carries it through untouched on submit, so a
`dhl.track_parcel` call is never wiped by an unrelated options edit.

*Polling* — unconditional and status-driven, no user-facing interval, and the
mid tier is **30 min** rather than the scaffold's 45. Account-based, so it never
fully stops.

*Module layout* — two setup-flow sources (`account/`, `tracking/`), each a
self-contained package with its own `client.py`/`coordinator.py`/`parcels.py`
(mirroring `ha-bpost`/`ha-usps`); the domain root dispatches on `CONF_SOURCE`
and otherwise carries no source-specific logic. Within `account/`, the
country-split build means several of *its* modules dispatch instead of
implementing:

| File | Carrier-specific? |
|---|---|
| `__init__.py` (source dispatch, setup/unload for both) | no |
| `api.py` / `coordinator.py` / `parcels.py` (domain root) | no — compatibility re-exports onto `account/client.py` etc., for the pre-split public import path |
| `const.py` | partly (shared contract + DE-specific/PL-specific/tracking-specific constants) |
| `config_flow.py` (source menu + account's country router/OIDC/SMS flows + tracking's setup/options steps) | **yes** — no precedent elsewhere in the suite |
| `services.py` (`dhl.track_parcel`/`untrack_parcel`) | no — account-source only; filters to account entries, never picks a tracking entry |
| `account/client.py` (transport dispatcher; error types in `const.py`) | no — dispatches into `account/countries/de/` and `account/countries/pl/` |
| `account/parcels.py` (`normalize_parcel`/`is_outgoing` country dispatch, sort, delivered-filter) | no — dispatches into `account/countries/de/` and `account/countries/pl/` |
| `account/coordinator.py` | partly (tracked-code merge, rate-limit/stall WARNINGs, PL cookie-jar persistence) |
| `account/countries/de/__init__.py` (transport, ladder, `normalize_parcel_de`) | **yes** |
| `account/countries/de/session.py` (OIDC token lifecycle) | **yes** |
| `account/countries/pl/__init__.py` (transport, status maps, `normalize_parcel_pl`) | **yes** |
| `account/countries/pl/session.py` (Altcha solver, SMS auth, cookie-pair session lifecycle) | **yes** |
| `tracking/__init__.py` (shape classification, code routing) | **yes** |
| `tracking/gateway.py` (client + `normalize_parcel_gateway`) | **yes** |
| `tracking/express.py` (client + `normalize_parcel_express`) | **yes** |
| `tracking/budget.py` (`RequestBudget` token bucket) | no — ported from `ha-ups`'s model |
| `tracking/coordinator.py` (routing, Express queue/throttle, events) | partly (the queue/throttle mechanics mirror `ha-ups`, the routing and both normalizers don't) |
| `tracking/parcels.py` (per-backend dispatch, sort, filters) | no |

## Load-bearing tracking decisions — do not refactor away

**Routing is a classification of the code, never a fallback chain tried
against both backends.** A gateway-shaped code that the gateway can't resolve
must not also try Express, and vice versa — see `ARCHITECTURE.md`'s
"Tracking source" section for why. The one exception is a code matching
neither known shape, which tries the gateway first and only falls back to
Express if the gateway can't resolve it either.

**The Express half is a shared single-token-bucket queue, not one budget per
code.** `tracking/coordinator.py` spends at most one Express request per poll
cycle, chosen by `_express_queue` (ported from `ha-ups`'s `_fetch_queue` —
never-attempted-first, then an overdue band, then `_QUEUE_PRIORITY`). **Do
not** give each tracked Express code its own budget; that was measured to
answer only a handful of requests total, not per code.

**A `DRG10012` match in the response body is a stand-down signal, matched on
the body, never the HTTP status alone.** It has been observed riding a `503`,
but the mechanics research is explicit that it is not guaranteed to. Treat it
as `DHLExpressThrottledError`, never as a fatal error — a stand-down must be
scheduled at least `DHL_EXPRESS_REQUEST_BUDGET_REFILL_SECONDS` out (`ha-ups`'s
`_standing_down`/`_schedule` split exists to stop a shorter one polling
straight back into the cooldown it's waiting out; this repo reuses that
split).

**A 401/403 from the Express endpoint is `DHLExpressCredentialError`, never
`DHLAuthError`.** There is no user credential to reauthenticate with, so this
must never reach Home Assistant's reauth flow — the coordinator disables
Express fetching for the rest of that running entry and logs one WARNING
instead.

**The gateway's request batching must match on the `barcode` field, never
array position.** An unresolved code in a batch is silently dropped from the
response, not erred — `tracking/gateway.py::async_fetch_gateway` builds its
result dict keyed on each returned item's own `barcode`.

**Gateway events map on the fine `status` first, then `category`.**
`tracking/gateway.py::_map_event` drives both the parcel status (last event)
and every history entry. `_STATUS_MAP` is ha-dhl-nl's ECOMMERCE table, because
the category alone cannot express `at_pickup_point` or `returning`, and
`INTERVENTION` covers both a harmless reschedule and a cancelled delivery.
**Do not add `PARCEL_RETURNED_FROM_ROUTE` or `PARCEL_READY_FOR_RETURN_TO_HUB`
back as `returning`.** On this gateway both show up in the normal outbound
flow of a parcel dropped off at a ParcelShop, which then went on to be
delivered (a real parcel, 2026-09-28). Only an unmapped *category* warns: the
CDEx/Express fine statuses (`SCAN_OK_GATEWAY`, `PROCESSED_AT_LOCATION`, …)
are not in the table and fall back to their category silently.

**`RETURNED_TO_SHIPPER` is checked on the *last* event only.** A return
shipment's log has been observed to resume with further `UNDERWAY` events
after a `RETURNED_TO_SHIPPER` event — its presence anywhere in the log is not
"stop watching this parcel".

**Express checkpoints map on their start, and only for text seen on a real
parcel.** The checkpoints carry free English text and no code, with the
facility appended in capitals (`Processed at MILAN - MALPENSA - ITALY`), so
`_CHECKPOINT_PREFIXES` matches the start of the text. Anything else is
`unknown` with one WARNING per description, with the facility stripped so a new
location doesn't warn again. The top-level `status` is only filled on
delivery; before that the parcel status is the newest checkpoint's, falling
back to `in_transit` on a future EDD. A code with no checkpoints at all (the
not-yet-fetched placeholder) is `unknown` without a warning.

**Interval scheduling stays split.** `tracking/coordinator.py` reuses
`account/coordinator.py`'s `compute_poll_interval` for the whole coordinator's
cadence (the gateway is cheap and unthrottled) and only overrides it when an
active Express stand-down outlasts that cadence. **Do not** make the whole
coordinator's interval budget-driven the way `ha-ups`'s is — `ha-ups` has no
second, cheap backend to poll on a normal cadence; this repo does, and most
installs will have no Express-shaped codes tracked at all.

**The tracking coordinator's `Store` is what makes the budget a budget.**
`async_load_cache()` runs before the first refresh and `_persist_cache()`
after every poll. Drop either and every restart spends an Express request the
backend never granted, or cuts a running stand-down short. The stand-down is
persisted as a UTC deadline, never a duration. Cached raw payloads without the
backend marker are discarded on load.

**`tracking/parcels.py::normalize_parcel` dispatches on a private
`_dhl_backend` marker the coordinator stamps onto every raw payload before
normalizing, and strips again before it reaches `raw`.** Don't let that
marker leak into a published parcel's `raw` field, and don't try to infer the
backend from the payload shape instead — a placeholder for an unfetched code
has too little shape to infer from reliably.

## Running tests

```
python -m pytest tests/ --cov=custom_components.dhl
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README, `ARCHITECTURE.md` and this file in
the same commit; API mechanics go to `carrier-research/dhl/api/`, never
here.
