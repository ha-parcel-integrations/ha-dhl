# Working in this repository

Home Assistant custom integration for **DHL Paket (Germany)** parcel tracking.
Distributed via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template** — everything outside *Carrier-specific
notes* is suite-wide; when in doubt check the template or a sibling repo.
No DTO layer.

**Country-split from day one.** `ha-dhl` is the country-split DHL repo — DE
today, with the layout (`custom_components/dhl/countries/<code>/`) already in
place for a future second country, mirroring `ha-gls`'s own split. DHL
eCommerce NL is a *separate*, already-released repo
([`ha-dhl-nl`](https://github.com/ha-parcel-integrations/ha-dhl-nl)) — a
different backend, not something this repo extends. The maintainer's stated
intent is to eventually fold it in as `countries/nl/`, but that is a
repo-consolidation decision for later, not something to design toward here.

**Config flow is a country router from day one, mirroring the module
layout.** `async_step_user` only shows a country picker (`COUNTRIES` —
today just DE) and dispatches to `async_step_<code>`; `async_step_de` holds
the entire browser-paste OIDC dance and is otherwise unchanged from before
the router existed. NL's auth model (email/password, like `ha-dhl-nl`) has
nothing in common with DE's OAuth flow, so its step will look nothing like
`async_step_de` — no shared base class is worth building for two data
points. `unique_id` is `f"{country}:{subject}"`; reauth reads the country
back off the existing entry (`entry_data[CONF_COUNTRY]`), it never asks
again.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change the sort/first-refresh; touch unmapped-status logging | *Parcel contract* — exact key set, units, sort, events + suppression; `test_parcels.py::test_normalize_publishes_exactly_the_canonical_keys` guards the key set |
| change which optional field this carrier populates vs. always returns `None` | Update `const.py`'s `CAPABILITIES` in the same commit — it feeds the comparison table on the docs site, so a field that starts (or stops) coming back non-null and isn't reflected there is a wrong claim on the website, not just a stale comment |
| ship anything while below 1.0.0 (unconfirmed data) | *Pre-1.0 releases* — one-shot WARNINGs for every guessed shape/code |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client, sync requests) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` and half-sets-up the
  entry. Runtime-only; tests don't catch a regression.
- **Setup stale-entity sweep is scoped to `domain == "sensor"` and skips
  `non_parcel_unique_ids`** — else it deletes the refresh button / the
  summary+diagnostic sensors. Add a new non-parcel sensor's unique_id to the set.
- **Per-parcel sensors are removed by the summary sensor** via
  `entity_registry.async_remove` (self-removal races and leaves ghosts).

## Carrier-specific notes

**API mechanics live in `carrier-research/dhl/api/dhl-de/` (private research
repo)** — the OIDC discovery/token endpoints, the `int-verfolgen/data/search`
inbox+by-number envelope, the `fortschritt` ladder and every contested field.
Do not duplicate them here.

**`payload: confirmed`, 1.0.0.** A real account's populated `sendungen`
elements have been observed on the wire, both via the account inbox and the
by-number search. Every mapping decision in `countries/de/__init__.py` stays
guarded with a one-shot `WARNING` (`issues/new?template=
unrecognised_status.yml`) regardless — the free-text status vocabulary and
the delivery-window shape are still open — but the payload itself is no
longer a reconstruction.

**Requests must originate from a German IP.** DHL's tracking endpoint does
not answer a non-German client correctly — not a bot-detection quirk tied to
being a script rather than a browser, a country-level thing. This is a
non-issue for the integration's actual users (a DHL Paket customer's own
Home Assistant is already in Germany) but matters for anyone developing or
testing this repo from elsewhere: use a German VPN/VM, or expect every
request to stall until `DHL_DE_REQUEST_TIMEOUT_SECONDS` and fail.

**`dhli` cookie plus plain headers — no `dhlcs`, no desktop-browser
spoofing.** A `dhlcs` cookie (decoded from the ID token's `sub` claim) is not
needed. A header set is: a bare aiohttp request (no `User-Agent`, no
`Accept`) to `www.dhl.de` stalls until timeout rather than getting a fast
response, so `DHL_DE_TRACKING_HEADERS` sends a plain, realistic set — no
desktop-Chrome `User-Agent`, no `Referer`.

**Polling is unconditional and status-driven, no user-facing interval.**
`coordinator.compute_poll_interval()`: a quiet window 00:00–06:00 local time
(one wake near each end), a 15-minute hot tier once any active parcel is
`out_for_delivery` (immediately if `planned_from` is missing, otherwise
starting 1h before it), a 30-minute mid tier otherwise, and a small
per-install stagger so installs don't all poll in sync. Recomputed at the
end of every `_async_update_data()`. Account-based, so it never fully
stops — there's always the next poll's new-shipment-detection value.

**The OIDC client id, redirect URI and login `claims` all matter for whether
the account-inbox call returns real data, not just for login itself.**
Logging in with the wrong client id succeeds (tokens issued, no error
anywhere) but silently gets an empty account inbox every poll — confirmed
live, repeatedly, until the client id, redirect URI and `claims` request
parameter (`DHL_DE_LOGIN_CLAIMS` — `post_number` specifically) all matched a
known-working client. That client authenticates at the token endpoint with
an empty Basic-auth secret, not PKCE-only. Don't change any of these four
together without a live re-test against a real account with real parcels —
a change that still logs in without error proves nothing about whether the
inbox comes back populated.

- **Login is a one-time browser hop, then headless.** `config_flow.py`
  builds its own PKCE authorization URL (`countries/de/session.py`), the
  user logs in in a real browser and pastes back the
  `dhllogin://…?code=…` redirect their browser could not open. The stored
  credential is the **refresh token**, never a password; the authorization
  code and PKCE verifier are discarded the moment the exchange finishes.
  `session.py` refreshes the ID token before every use (margin: 5 min, short
  relative to the confirmed 1800s token lifetime) and forces exactly one
  refresh on a 401 from `www.dhl.de` — never a retry loop. A rejected
  refresh token raises `DHLDeAuthError`, converted to `DHLAuthError` at the
  transport layer and to `ConfigEntryAuthFailed` by the coordinator, so HA
  starts reauth rather than retrying forever.
- **Every DE request carries an explicit 30s timeout**
  (`DHL_DE_REQUEST_TIMEOUT_SECONDS`, `const.py`) — discovery, token exchange
  and the tracking GET all set it. Observed live 2026-08-23: with no timeout,
  a stalled first-refresh hung the full aiohttp default (300s) with nothing
  logged (HA suppresses tracebacks on a config entry's first refresh), and
  by the time it gave up, the refresh token had likely already rotated
  server-side — the retry got `invalid_grant` on the now-dead token. A short
  timeout turns that into a fast, retryable failure instead of a silent
  5-minute hang plus a forced reauth. `config_flow.py` catches `TimeoutError`
  alongside `aiohttp.ClientError` for the same reason — a total-timeout
  raises the former, not the latter.
- **A rotated refresh token is persisted.** Some OIDC providers issue a new
  refresh token on every refresh call; `DHLDeSession` flags this
  (`pop_refresh_token_changed`) and the coordinator writes it back to
  `entry.data` after every poll, or a restart would resume with a stale
  token.
- **One endpoint, two models.** `client.async_get_incoming()` is the account
  inbox (auto-import, archived elements filtered per §5b, with the
  empties-the-list fallback). `client.async_get_by_number()` is the
  `dhl.track_parcel` service's by-number lookup — for a parcel the logged-in
  account's own inbox does not carry. The coordinator merges: every tracked
  code not already present in the inbox is fetched by number and folded into
  the same active/delivered split, sort and event-firing path.
- **The status ladder is a progress bar, not a vocabulary.** `fortschritt`
  0-5, bounded by `maximalFortschritt` (defended against being absent/≤0).
  Rung 2 ("Im Zustellzentrum") was contested between two third-party sources
  (`registered` vs `in_transit`); a third source (Versand-HA, same
  mechanism) settled it as `in_transit`, consistent with how `ha-dhl-nl`
  maps an equivalent depot/hub scan — `REGISTERED` is reserved for before
  the carrier has physically scanned the parcel at all. **`at_pickup_point` and
  `problem` have no known mechanism at all** — no source names a
  Packstation/Filiale field. The coordinator warns when a parcel stays at
  `out_for_delivery` across more than one poll, the best available proxy for
  a silently-misreported pickup arrival. **`AT_PICKUP_POINT` is therefore
  unreachable in `_LADDER` and `sensor.py` deliberately has no
  `awaiting_pickup` sensor** — this is the exemption `CONVENTIONS.md`'s
  status-vocabulary section asks a carrier to state explicitly, not an
  oversight.
- **Every genuinely contested field codes both branches**, per the plan's
  own instruction, rather than picking one: the delivered flag (read
  `istZugestellt` if present, else derive from the ladder, warn on
  disagreement), `raw_status` (prefer `.status`, log `.kurzStatus`), and the
  delivery window (prefer the `Von`/`Bis` pair, fall back to the singular
  `zustellzeitfenster`/`zustelldatum`).
- **`receiver` stays `None`.** `.zustellung.empfaenger.name` is single-source
  and means "who physically took the parcel" (Packstation/Filiale/neighbour),
  not necessarily the addressee — it is not mapped to `receiver` until a
  tester export confirms the semantics. It is still present, redacted, in
  `raw`.
- **`weight`/`dimensions`/`pickup_point` are always `None`** — no source
  names any of the three. Keep `CAPABILITIES` in `const.py` in sync if that
  ever changes.
- **Diagnostics redact leaves, never containers.** `TO_REDACT` in
  `diagnostics.py` deliberately does not include `zustellung`/`empfaenger` —
  those are objects, and `async_redact_data` replaces a redacted key's whole
  value, which would collapse `sendungsdetails.zustellung.empfaenger.name`'s
  nesting into a string. Redacting the leaf `name` key reaches the PII
  without losing the structure a tester's export needs. `CONF_TRACKED_CODES`
  is redacted by hand (a bare list of strings has no key for
  `async_redact_data` to match).
- **`dhl.track_parcel` targets a config entry, not a hub.** Unlike an
  account-less carrier's `track_parcel`, more than one configured DHL
  account needs `config_entry_id` to disambiguate — see `services.py`.

## Options and reloads

The options flow is one sectioned form (`data_entry_flow.section`); changes apply
without a restart. Two models, **do not mix them**:
- **Account-less carriers** (the default) apply changes live: an update listener
  retunes `coordinator.update_interval` and calls `async_request_refresh()`, so
  added/removed parcel sensors appear immediately.
- **Account-based carriers** call `async_schedule_reload` on submit and register
  **no** update listener. Combining a listener with a reload-on-update flow is
  deprecated, an error in HA 2026.12+. `options.async_init`'s form itself
  never touches `CONF_TRACKED_CODES` (that's `services.py`'s job, below) —
  the options schema carries it through untouched on submit so a
  `dhl.track_parcel` call is never wiped by an unrelated options edit.

DHL is account-based (`async_schedule_reload`, no update listener) but is
also the suite's first account-based carrier with a `track_parcel` service
for its by-number half — `services.py` nudges the coordinator directly with
`async_request_refresh()` after an add/remove, rather than relying on either
mechanism above.

The user-tunable poll interval is a deliberate HACS divergence (see
CONVENTIONS.md); a carrier that throttles is generated with a fixed cadence and no
polling option at all.

## Module layout

| File | Carrier-specific? |
|---|---|
| `api.py` (transport dispatcher; error types live in `const.py`) | no — dispatches into `countries/de/` |
| `const.py` (domain, URLs, `ParcelStatus`, option keys) | partly (DE-specific URLs/OIDC constants) |
| `parcels.py` (`normalize_parcel` country dispatch, sort, delivered-filter — pure, no I/O) | no — dispatches into `countries/de/` |
| `coordinator.py` (fetch, tracked-code merge, cache, event firing) | partly (tracked-code merge, rate-limit/stall WARNINGs) |
| `config_flow.py` (browser-paste OIDC flow) | **yes** — no precedent elsewhere in the suite |
| `sensor.py` / `button.py` / `calendar.py` / `device_trigger.py` | no |
| `diagnostics.py` | partly (`TO_REDACT`) |
| `services.py` (`track_parcel` / `untrack_parcel`) | no — DHL is account-based and still carries this, unlike other suite carriers where it's account-less-only |
| `countries/de/__init__.py` (transport, status map, `normalize_parcel_de`) | **yes** |
| `countries/de/session.py` (OIDC token lifecycle) | **yes** |

`parcels.py` is deliberately free of I/O and HA objects so the per-carrier part
stays unit-testable without Home Assistant. Config: `ConfigEntry.runtime_data`
(typed, no `hass.data`), `PARALLEL_UPDATES = 0`, coordinator takes
`config_entry=entry`. `aiohttp.ClientError` is caught **per parcel** in the gather
loop (one bad parcel doesn't fail the poll) but **not** around the whole update
(the coordinator wraps that). Entities: `has_entity_name` + `translation_key`,
`icons.json`, translated units, `_attr_attribution`, `_unrecorded_attributes` on
anything with a parcel list or `raw`. Over-redact diagnostics — they get pasted
into public issues.

## Running tests

```
python -m pytest tests/ --cov=custom_components.dhl
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README + this file + `docs/` in the same
commit; the API reference lives in this carrier's own directory in the private
`carrier-research/<slug>/api/`, never in this repo.
