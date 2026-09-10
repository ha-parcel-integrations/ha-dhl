# Architecture

How `ha-dhl` is built and why it is built that way. `CLAUDE.md` is the short
list of things not to get wrong; this file is the structure and the evidence
behind it. API mechanics — the OIDC discovery/token endpoints, the
`int-verfolgen/data/search` envelope, the `fortschritt` ladder and every
contested field — live in the private `carrier-research/dhl/api/dhl-de/` and
are never copied here.

Three things shape this repo. It is **country-split from day one**, with only
DE built. Its auth is a **one-time browser hop** producing a refresh token, not
a password. And a large share of its payload mapping is **inferred from
third-party sources** rather than observed, so nearly every decision codes both
branches and warns.

## Country split

`ha-dhl` is the country-split DHL repo — DE today, with the
`custom_components/dhl/countries/<code>/` layout already in place for a second
country, mirroring `ha-gls`. DHL eCommerce NL is a *separate*, already-released
repo ([`ha-dhl-nl`](https://github.com/ha-parcel-integrations/ha-dhl-nl)) on a
different backend, not something this repo extends. The maintainer's stated
intent is to eventually fold it in as `countries/nl/`, but that is a
repo-consolidation decision for later — **do not design toward it here.**

**The config flow is a country router**, mirroring the module layout.
`async_step_user` shows only a country picker (`COUNTRIES` — today just DE) and
dispatches to `async_step_<code>`; `async_step_de` holds the entire
browser-paste OIDC dance. NL's auth model (email/password, like `ha-dhl-nl`)
has nothing in common with DE's OAuth flow, so its step will look nothing like
`async_step_de` — **no shared base class is worth building for two data
points.** `unique_id` is `f"{country}:{subject}"`; reauth reads the country back
off the existing entry (`entry_data[CONF_COUNTRY]`) and never asks again.

## Project layout

```
custom_components/dhl/
├── __init__.py          setup, session + coordinator wiring, first refresh
├── api.py               transport dispatcher; error types live in const.py
├── const.py             COUNTRIES, ladder, headers, timeouts, CAPABILITIES
├── coordinator.py       poll loop, by-number merge, in/outgoing split, events
├── parcels.py           per-country dispatch (normalize_parcel, is_outgoing), sort, filters
├── config_flow.py       country router + the DE browser-paste OIDC flow
├── sensor.py            summary, per-parcel, outgoing and diagnostic sensors
├── button.py            refresh button
├── calendar.py          read-only deliveries calendar
├── services.py          dhl.track_parcel / dhl.untrack_parcel (by-number)
├── device.py / device_trigger.py / diagnostics.py
└── countries/
    └── de/
        ├── __init__.py  transport, ladder mapping, normalize_parcel_de, is_outgoing_element
        └── session.py   OIDC lifecycle: PKCE, refresh, rotation
```

`PLATFORMS` is `[Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]`.

Concern-level files stay top-level and dispatch into the country package;
`countries/de/` owns everything DE-specific.

## Authentication

**Login is a one-time browser hop, then headless.** `config_flow.py` builds a
PKCE authorization URL (via `countries/de/session.py`); the user logs in in a
real browser and pastes back the `dhllogin://…?code=…` redirect their browser
could not open. **The stored credential is the refresh token, never a
password**; the authorization code and PKCE verifier are discarded the moment
the exchange finishes.

`session.py` refreshes the ID token before every use — a 5-minute margin,
short relative to the confirmed 1800 s lifetime — and forces **exactly one**
refresh on a 401 from `www.dhl.de`, never a retry loop. A rejected refresh
token raises `DHLDeAuthError`, converted to `DHLAuthError` at the transport
layer and to `ConfigEntryAuthFailed` by the coordinator, so HA starts reauth
instead of retrying forever.

**A rotated refresh token is persisted.** Some OIDC providers issue a new
refresh token on every refresh call; `DHLDeSession` flags this
(`pop_refresh_token_changed`) and the coordinator writes it back to
`entry.data` after every poll — otherwise a restart resumes with a stale token.

### The client id, redirect URI and `claims` are load-bearing for *data*

Logging in with the wrong client id **succeeds** — tokens issued, no error
anywhere — but silently returns an empty account inbox on every poll. That was
confirmed live, repeatedly, until the client id, redirect URI and the `claims`
request parameter (`DHL_DE_LOGIN_CLAIMS`, `post_number` specifically) all
matched a known-working client. That client authenticates at the token endpoint
with an **empty Basic-auth secret**, not PKCE-only.

**Do not change any of these four without a live re-test against a real account
with real parcels.** A change that still logs in without error proves nothing
about whether the inbox comes back populated.

## Transport quirks

**Requests must originate from a German IP.** DHL's tracking endpoint does not
answer a non-German client correctly — a country-level restriction, not
bot-detection tied to being a script. This is a non-issue for real users (a DHL
Paket customer's Home Assistant is already in Germany) but matters for anyone
developing or testing from elsewhere: use a German VPN or VM, or expect every
request to stall until `DHL_DE_REQUEST_TIMEOUT_SECONDS` and fail.

**`dhli` cookie plus plain headers — no `dhlcs`, no desktop-browser spoofing.**
A `dhlcs` cookie (decoded from the ID token's `sub` claim) is not needed. But a
bare aiohttp request with no `User-Agent` and no `Accept` stalls until timeout
rather than failing fast, so `DHL_DE_TRACKING_HEADERS` sends a plain, realistic
set — no desktop-Chrome `User-Agent`, no `Referer`.

**Every DE request carries an explicit 30 s timeout**
(`DHL_DE_REQUEST_TIMEOUT_SECONDS`): discovery, token exchange and the tracking
GET all set it. Observed live 2026-08-23: with no timeout, a stalled first
refresh hung for the full aiohttp default (300 s) with nothing logged — HA
suppresses tracebacks on a config entry's first refresh — and by the time it
gave up, the refresh token had likely already rotated server-side, so the retry
got `invalid_grant` on a dead token. A short timeout turns that into a fast,
retryable failure instead of a silent five-minute hang plus a forced reauth.
`config_flow.py` catches `TimeoutError` alongside `aiohttp.ClientError` for the
same reason: a total-timeout raises the former, not the latter.

## One endpoint, two models

`client.async_get_incoming()` is the **account inbox** (auto-import, archived
elements filtered, with the empties-the-list fallback).
`client.async_get_by_number()` is the **by-number lookup** behind
`dhl.track_parcel`, for a parcel the logged-in account's own inbox does not
carry.

The coordinator merges them: every tracked code not already present in the
inbox is fetched by number and folded into the same active/delivered split,
sort and event-firing path.

**`dhl.track_parcel` targets a config entry, not a hub.** Unlike an
account-less carrier's `track_parcel`, more than one configured DHL account
needs `config_entry_id` to disambiguate.

## The status ladder is a progress bar, not a vocabulary

`fortschritt` runs 0–5, bounded by `maximalFortschritt` (defended against being
absent or ≤ 0). Rung 2 ("Im Zustellzentrum") was contested between two
third-party sources — `registered` vs `in_transit`. A third source (Versand-HA,
same mechanism) settled it as `in_transit`, consistent with how `ha-dhl-nl` maps
an equivalent depot/hub scan: `REGISTERED` is reserved for before the carrier
has physically scanned the parcel at all.

**`at_pickup_point` and `problem` have no known mechanism at all** — no source
names a Packstation/Filiale field. The coordinator warns when a parcel stays at
`out_for_delivery` across more than one poll, the best available proxy for a
silently-misreported pickup arrival.

**`AT_PICKUP_POINT` is therefore unreachable in `_LADDER`, and `sensor.py`
deliberately has no `awaiting_pickup` sensor.** This is the exemption
`CONVENTIONS.md` asks a carrier to state explicitly, not an oversight.

### Contested fields code both branches

Rather than picking one reading, each genuinely contested field implements both
and warns on disagreement:

| Field | Primary | Fallback |
|---|---|---|
| delivered flag | `istZugestellt` when present | derive from the ladder; warn on disagreement |
| `raw_status` | `.status` | log `.kurzStatus` |
| delivery window | the `Von`/`Bis` pair | singular `zustellzeitfenster` / `zustelldatum` |

**`payload: confirmed`, 1.0.0.** A real account's populated `sendungen` elements
have been observed on the wire, via both the account inbox and the by-number
search. Every mapping decision still carries a one-shot `WARNING` with an
`issues/new?template=unrecognised_status.yml` link, because the free-text status
vocabulary and the delivery-window shape remain open — but the payload itself is
no longer a reconstruction.

## `sender` / `receiver`, and an open risk

Both come from `sendungsinfo.sendungsname`, keyed on `sendungsrichtung` (issue
#2, reported live): `ANKOMMEND` / `EINGEHEND` (incoming — Versand-HA names
`EINGEHEND` alongside `ANKOMMEND`) means the name is the **sender**; `AUSGEHEND`
means it is the **recipient**. An unrecognised direction warns once and leaves
both `None` rather than guessing.

**Open risk, unconfirmed either way.** Versand-HA's own comment on this mapping
says DHL's *anonymous* by-piececode search returns `ANKOMMEND` for every parcel
regardless of true direction, sender's own shipments included — bad enough that
they ship a manual per-shipment override. Our by-number path
(`async_get_by_number_envelope`) hits the same `piececode`-keyed endpoint but
authenticated with the `dhli` cookie, unlike their anonymous call. Issue #2's
confirmed export came from the **account-inbox** path only, so whether the
authenticated by-number call shares the quirk is still open.

`.zustellung.empfaenger.name` stays a separate, **unused** source: it is
single-source and means "who physically took the parcel"
(Packstation/Filiale/neighbour), not necessarily the addressee. It is not mapped
to `receiver` until a tester export confirms the semantics, and remains present
but redacted in `raw`.

## Outgoing parcels come from the same list

Unlike `ha-dhl-nl`'s `DhlSentShipmentsCoordinator` (a genuinely separate API),
DE has **one endpoint**, and `sendungsrichtung` splits it.
`is_outgoing_element()` classifies each raw `sendungen[]` element *before*
normalization, `parcels.is_outgoing()` dispatches per country, and
`coordinator.py` partitions `elements` into incoming/outgoing before calling
`normalize_parcel` on each half.

There is **no new canonical parcel key** for direction — the suite's parcel
contract lists none, and `ha-dhl-nl`'s own `isReturn`/`type` split works the
same way, on the raw payload rather than the normalized one.
`coordinator.outgoing` / `.delivered_outgoing` mirror `.data` / `.delivered`,
feeding `DHLOutgoingParcelsSensor` / `DHLOutgoingDeliveredSensor`. There are no
per-parcel outgoing sensors, matching ha-dhl-nl's summary-only pattern.

**`status` is force-`UNKNOWN` for every outgoing parcel** (one-shot
`_warn_outgoing_status_unconfirmed_once`): the ladder was calibrated purely
against incoming reconstruction sources, so applying it to an `AUSGEHEND`
element would be a guess. `retoure` / `ruecksendung` can still override to
`RETURNING`, since that is a separate, direction-agnostic flag.

Because `status` can never reach `DELIVERED` for outgoing parcels,
`_fire_outgoing_change_events` detects the terminal hop from the
independently-derived `delivered` bool instead of `status == DELIVERED`. **Do
not "simplify" this back to mirroring `_fire_change_events`** — the two key
delivery detection differently on purpose. Outgoing events
(`dhl_outgoing_parcel_status_changed` / `_delivered`) have no `registered` or
`delivery_time_changed` counterpart, matching ha-dhl-nl.

**Doubly unconfirmed — this may ship inert.** Neither issue #2 nor Versand-HA
has ever actually observed a populated `AUSGEHEND` element, and Versand-HA's
anonymous by-piececode search (same params as `async_get_by_number_envelope`)
returns `ANKOMMEND` unconditionally. The outgoing sensors may legitimately read
0 forever until a real export proves otherwise. Shipping the pipeline
pre-guarded rather than waiting is a deliberate decision, not an oversight.

## Polling

Unconditional and status-driven — **no user-facing interval**.
`coordinator.compute_poll_interval()` recomputes at the end of every
`_async_update_data()`:

- a quiet window 00:00–06:00 local time, with one wake near each end;
- a **15-minute hot tier** once any active parcel is `out_for_delivery`
  (immediately when `planned_from` is missing, otherwise from 1 h before it);
- a **30-minute mid tier** otherwise;
- a small per-install stagger so installs don't poll in sync.

Account-based, so it **never fully stops** — the next poll is what detects a new
shipment.

## Diagnostics

**Redact leaves, never containers.** `TO_REDACT` deliberately excludes
`zustellung` / `empfaenger`: those are objects, and `async_redact_data` replaces
a redacted key's whole value, which would collapse
`sendungsdetails.zustellung.empfaenger.name`'s nesting into a string. Redacting
the leaf `name` key reaches the PII without destroying the structure a tester's
export needs.

`CONF_TRACKED_CODES` is redacted by hand — a bare list of strings has no key for
`async_redact_data` to match.

## Fields

`weight`, `dimensions` and `pickup_point` are always `None` — no source names
any of the three. Keep `CAPABILITIES` in `const.py` in sync if that ever
changes.
