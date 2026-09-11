# Working in this repository

Home Assistant custom integration for **DHL Paket (Germany)** parcel tracking.
Distributed via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template**. Country-split from day one — DE and PL
are implemented. No DTO layer.

Three places hold the knowledge, and they do not overlap:

| What | Where |
|---|---|
| How this integration is built, and why it is built that way | [`ARCHITECTURE.md`](ARCHITECTURE.md) — read it before touching the OIDC session, the `fortschritt` ladder, the in/outgoing split, or `countries/de/` |
| Endpoint mechanics, status vocabularies | `carrier-research/dhl/api/dhl-de/` (private repo) — the OIDC discovery/token endpoints, the `int-verfolgen/data/search` envelope, the `fortschritt` ladder and every contested field. **Never** duplicated into this repo |
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

**`AT_PICKUP_POINT` is unreachable in `_LADDER`, so there is deliberately no
`awaiting_pickup` sensor.** No source names a Packstation/Filiale field. This is
the exemption `CONVENTIONS.md` asks for explicitly, **not an oversight**.

**Contested fields code both branches and warn — don't collapse them to one.**
The delivered flag (`istZugestellt` else derive from the ladder, warn on
disagreement), `raw_status` (`.status`, log `.kurzStatus`), and the delivery
window (`Von`/`Bis` pair, else singular `zustellzeitfenster`/`zustelldatum`).

**Outgoing keys delivery detection differently from incoming — on purpose.**
`status` is force-`UNKNOWN` for every outgoing parcel (the ladder was calibrated
against incoming sources only), so `_fire_outgoing_change_events` detects the
terminal hop from the independently-derived `delivered` bool, not
`status == DELIVERED`. **Do not "simplify" this back to mirroring
`_fire_change_events`.** The outgoing pipeline may legitimately read 0 forever —
no source has ever observed a populated `AUSGEHEND` element — which is a
deliberate ship-pre-guarded decision.

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

**`weight`/`dimensions`/`pickup_point` are always `None`** — no source names
any of them. Keep `const.py`'s `CAPABILITIES` in sync if that changes.

**Do not design toward folding in `ha-dhl-nl`.** It is a separate released repo
on a different backend. Folding it in as `countries/nl/` is a later
repo-consolidation decision, and NL's auth model shares nothing with DE's — no
shared config-flow base class is worth building for two data points.

## Load-bearing PL decisions — do not refactor away

**`menuTimelineLabel.status` and the raw `status` (`TT_*`/`SP_*`) code are two
different, both-closed enums — never conflate them.** `_RAW` in
`countries/pl/__init__.py` is the primary source for canonical `status`; the
9-value coarse ladder (`_LADDER`) is only a fallback for a raw code that isn't
in `_RAW` yet (`carrier-research/dhl/api/dhl-pl/tracking.md`, "the coarse ladder").
The 22-name `ShipmentStatusName` timeline (`DeliveredToLocker`,
`RetrievedFromPoint`, …) is a **third**, closed enum whose field in the wire
payload was never confirmed by research — do not key `_LADDER` on those names;
a past version of this file did, and `menuTimelineLabel.status` never actually
carries them, so that mapping silently never fired.

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
persisting the rotated jar after every successful poll (`coordinator.py`) is
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

*Module layout* — country-split build, so several modules dispatch instead of
implementing:

| File | Carrier-specific? |
|---|---|
| `api.py` (transport dispatcher; error types in `const.py`) | no — dispatches into `countries/de/` and `countries/pl/` |
| `const.py` | partly (DE-specific URLs/OIDC constants, PL-specific Mój DHL constants) |
| `parcels.py` (`normalize_parcel`/`is_outgoing` country dispatch, sort, delivered-filter) | no — dispatches into `countries/de/` and `countries/pl/` |
| `coordinator.py` | partly (tracked-code merge, rate-limit/stall WARNINGs, PL cookie-jar persistence) |
| `config_flow.py` (country router + browser-paste OIDC flow + PL phone/SMS flow) | **yes** — no precedent elsewhere in the suite |
| `services.py` | no — but present on an account-based carrier, unlike the rest of the suite |
| `countries/de/__init__.py` (transport, ladder, `normalize_parcel_de`) | **yes** |
| `countries/de/session.py` (OIDC token lifecycle) | **yes** |
| `countries/pl/__init__.py` (transport, status maps, `normalize_parcel_pl`) | **yes** |
| `countries/pl/session.py` (Altcha solver, SMS auth, cookie-pair session lifecycle) | **yes** |

## Running tests

```
python -m pytest tests/ --cov=custom_components.dhl
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README, `ARCHITECTURE.md` and this file in
the same commit; API mechanics go to `carrier-research/dhl/api/dhl-de/`, never
here.
