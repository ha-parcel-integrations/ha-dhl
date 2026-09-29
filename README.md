# DHL Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-dhl.svg)](https://github.com/ha-parcel-integrations/ha-dhl/releases)
[![Downloads](https://img.shields.io/github/downloads/ha-parcel-integrations/ha-dhl/total.svg)](https://github.com/ha-parcel-integrations/ha-dhl/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks **DHL Paket** (Germany),
**DHL Parcel Polska**, and — via tracking codes, no account needed — DHL
Parcel's wider network and DHL Express. During setup, pick **Account** to log
in with your own DHL Kundenkonto or Mój DHL account (parcels import
automatically), or **Tracking codes** to add tracking numbers directly; each
code is routed automatically to whichever DHL backend can answer it.

Part of the [ha-parcel-integrations](https://ha-parcel-integrations.github.io/) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

**Account support:** Germany and Poland. DHL's Polish account setup uses a
nine-digit Polish mobile number and a one-time SMS code; the renewable session
cookie jar is stored locally, never the SMS code or a bearer token. Country
requests go through the [organisation discussion](https://github.com/ha-parcel-integrations/.github/discussions/new/choose).
DHL's Netherlands business is a separate integration,
[**ha-dhl-nl**](https://github.com/ha-parcel-integrations/ha-dhl-nl).

**Tracking-code support:** any DHL Parcel barcode (`3S…`, `JJD…`, `CR…`/`LX…`)
and any DHL Express air waybill (a bare 10-digit number), worldwide, plus
domestic DHL Freight Sweden shipment numbers and DHL parcel numbers that only
DHL's Polish tracking (Mój DHL) knows, such as a parcel sent from Poland to
another country — no account or postcode needed. See [Tracking codes](#tracking-codes) below.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Tracking codes](#tracking-codes)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Services](#services)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Automatic parcel import from your DHL Kundenkonto — no tracking codes to enter for your own parcels
- `dhl.track_parcel` / `dhl.untrack_parcel` services to also track a parcel that is not (yet) in your account's inbox
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `out_for_delivery` / `delivered` / …), DHL's own status text, the expected delivery window and a tracking deep-link
- Summary sensors: incoming parcels, next delivery, recently delivered parcels, outgoing parcels
- Read-only **Deliveries** calendar with the expected delivery windows
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor
- Structure-preserving diagnostics export — the tool this pre-release is finished with (see [Debugging](#debugging))

## Requirements

**Account setup:**

- A DHL Kundenkonto (a free DHL account) — the same account you use on
  dhl.de or in the DHL Paket app — or a Mój DHL account for Poland
- A browser to complete the one-time sign-in during setup (Germany only)
- **A German IP address for Germany.** DHL's tracking endpoint only answers
  requests that originate from Germany — this is normally not a concern (a
  DHL Paket customer's own Home Assistant is already reached from Germany),
  but it means the integration will not work over a non-German VPN, from a
  non-German cloud/VPS-hosted Home Assistant, or during development/testing
  from outside Germany.

**Tracking-code setup:** nothing beyond the tracking code itself — no
account, no login, no postcode.

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-dhl` as an **Integration**.
3. Install **DHL** and restart Home Assistant.

### Manual

Copy `custom_components/dhl` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

1. Go to **Settings → Devices & Services → Add Integration → DHL**.
2. Choose **Account** or **Tracking codes**.

### Account

1. Pick a country. For Poland, enter the Mój DHL mobile number and then the
   single SMS code DHL sends; the integration never resends it automatically.
2. The next form shows a sign-in link. Open it in a browser and log in with
   your DHL Kundenkonto.
3. Your browser will fail to open the final `dhllogin://…` redirect it lands
   on — **that is expected**. This address never appears in the address bar
   (no desktop browser has an app registered for it); you catch it in your
   browser's developer tools' Network tab instead. See
   [docs/finding-the-redirect-url.md](docs/finding-the-redirect-url.md) for
   step-by-step instructions for Chrome, Edge, Firefox and Safari. Paste the
   full address into the form.
4. Submit. Your account's parcels start appearing on the next poll.

Nothing is typed into Home Assistant itself except that pasted-back address —
your DHL password never passes through this integration.

#### Adding a parcel that is not in your account

Your DHL account inbox only shows parcels addressed to you. To also track a
parcel someone else is sending you (or one your account has not picked up
yet), call the [`dhl.track_parcel`](#services) service with its tracking
number, or use a [dashboard button](examples/dashboards/add_parcel_card.yaml).

### Tracking codes

Choosing **Tracking codes** creates a single tracking hub with no parcels yet
— add codes afterwards from **Configure → Incoming parcels** or **Outgoing parcels**. Nothing is validated
against DHL at add time; a code only resolves (or doesn't) on the next poll.

## Tracking codes

Each code you add is routed automatically to whichever DHL backend can
answer it — you never say which. A code is tried on one backend after the
other until one of them knows it, and is followed there from then on:

| Code shape | Tried on, in order |
|---|---|
| `3S…`, `JJD…`, `CR…`/`LX…` (DHL Parcel barcodes) | A keyless DHL Parcel gateway covering DHL's wider parcel network, not just Germany or the Netherlands; then Mój DHL's public tracking |
| A bare 10-digit number (a DHL Express air waybill) | The DHL Express tracking backend; then DHL Freight Sweden's public tracking (Mitt DHL) |
| Anything else | The DHL Parcel gateway; then Mój DHL's public tracking (codes of 11 characters or more); then the DHL Express backend |

**The DHL Express backend is intentionally slow to refresh — roughly once
every 40 minutes per tracked Express code, shared across however many you
track.**

## Options

Click **Configure** on the integration entry. An account hub shows one
sectioned form; a tracking hub shows a menu:

| Menu entry | Option | Default | Description |
|---|---|---|---|
| Incoming parcels *(tracking hub only)* | Tracking codes | — | Add or remove the tracking codes of parcels you expect. |
| Outgoing parcels *(tracking hub only)* | Tracking codes | — | Add or remove the tracking codes of parcels you sent. They are counted on the outgoing sensors, not the incoming ones. Entering a code here that is filed as incoming moves it. |
| Settings | Filter by / amount (Delivered parcels) | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Settings | Include status history (Parcel history) | off | Adds a `history` attribute per parcel with each status update. |

Polling isn't one of these settings. An account hub polls on a dynamic,
status-driven schedule (quiet overnight window, faster when a parcel is out
for delivery); a tracking hub follows the same schedule for its DHL Parcel
codes, with Express codes rationed separately as described above. See
[ARCHITECTURE.md](ARCHITECTURE.md) for the details.

## Removal

Standard HA removal applies: **Settings → Devices & Services → DHL → ⋮ → Delete**. Nothing is stored on DHL's side beyond the normal session your account already has.

## Sensors

| Entity | Description |
|---|---|
| `sensor.dhl_<hub>_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.dhl_<hub>_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.dhl_<hub>_next_delivery` | Earliest expected delivery moment across all active parcels |
| `sensor.dhl_<hub>_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `sensor.dhl_<hub>_outgoing_parcels` | Number of active outgoing parcels, full list under the `parcels` attribute |
| `sensor.dhl_<hub>_outgoing_delivered_parcels` | Recently delivered outgoing parcels (see the retention option) |
| `sensor.dhl_<hub>_last_successful_update` | Diagnostic: when DHL was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically.

Outgoing parcels are shipments DHL reports as sent *by* your account rather than to it — DHL exposes this as a `sendungsrichtung` field (`ANKOMMEND`/`EINGEHEND` incoming, `ABGEHEND` outgoing) on the same account-inbox listing incoming parcels come from, no separate endpoint. Their canonical `status` always reports `unknown`: the 0-5 progress ladder below was only ever confirmed against incoming shipments, so guessing what it means for an outgoing one isn't done — the raw DHL status text is still available as `raw_status`.

On a tracking hub DHL's tracking data cannot tell a parcel you sent from one you receive, so you choose: a code filed under **Outgoing parcels** is counted on the outgoing sensors and fires the outgoing events, with its normal status.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family, and its exact source depends on which DHL backend produced a given parcel.

**Account (Germany).** DHL Germany reports a coarse 0-5 progress ladder rather than a status vocabulary, so the mapping below is what that ladder can express, plus a Packstation arrival read from the parcel's delivery details. A Filiale arrival cannot currently be told apart from an ordinary "out for delivery" (see [Troubleshooting](#troubleshooting)).

| Status | Meaning |
|---|---|
| `registered` | Label created / picked up by DHL |
| `in_transit` | In DHL's network |
| `out_for_delivery` | On a delivery vehicle today |
| `at_pickup_point` | Ready to collect in a Packstation (Filiale not distinguishable — see Troubleshooting) |
| `delivered` | Delivered |
| `returning` | DHL reports the shipment as a return |
| `problem` | Not currently distinguishable — see Troubleshooting |
| `unknown` | A progress value we have not mapped yet |

**Tracking codes — DHL Parcel gateway.** The event category behind a barcode maps as: `DATA_RECEIVED` → `registered`, `UNDERWAY` → `in_transit`, `IN_DELIVERY` → `out_for_delivery`, `PROBLEM` → `problem`, `DELIVERED` → `delivered`; a `RETURNED_TO_SHIPPER` event maps to `returning` regardless of category. `at_pickup_point` is not currently mapped on this backend. Anything else falls back to `unknown`.

**Tracking codes — DHL Express.** `DELIVERED` maps to `delivered`. Before delivery the status follows the newest checkpoint: *Shipment is out with courier for delivery* is `out_for_delivery`; *Delivery attempt could not be completed*, *Delivery not accepted*, *Further consignee information needed* and *On hold awaiting for payment of shipment related fees* are `problem`; *Shipment information received* is `registered`; *Shipment Accepted*, *Shipment picked up*, *Processed at …*, *Arrived at DHL Sort Facility …*, *Arrived at DHL Delivery Facility …*, *Shipment has departed from a DHL facility …*, *Shipment is in transit to destination*, *Customs clearance status updated*, *Clearance processing complete at …*, *Payment is received and recorded for shipment related fees* and *Shipment is scheduled for delivery* are `in_transit`. Any other checkpoint is `in_transit` while the estimated delivery date is still ahead, otherwise `unknown`. No pickup-point state has been observed on this backend yet. Checkpoint times are the local time of the DHL facility; they carry that country's time zone when it has only one, and no offset otherwise.

**Tracking codes — DHL Freight Sweden.** Follows Mitt DHL's own status card: a collected shipment is `delivered`, one waiting too long at the service point is `returning`, a stopped one is `problem`, and one ready for collection is `at_pickup_point`. Otherwise the newest event decides: *OUT FOR DELIVERY* is `out_for_delivery`, a drop-off at the service point or locker is `at_pickup_point`, a return is `returning`, and terminal and transport events are `in_transit`. A shipment without events is `registered`. `pickup_point` is the service point's name.

**Tracking codes — Mój DHL.** Uses the same DHL Parcel Polska status codes as a Polish account: a delivered or collected parcel is `delivered`, one waiting in a locker or behind a notice is `at_pickup_point`, one handed to the courier is `out_for_delivery`, a return is `returning`, and delays and delivery problems are `problem`. This backend has no event log, so parcels found here have no `history`.

The carrier's own human-readable text is always available as `raw_status`.

## Events

The integration fires these on the event bus (also available as device triggers on the DHL device):

| Event | When |
|---|---|
| `dhl_parcel_registered` | A new parcel appears in the active list |
| `dhl_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `dhl_parcel_delivered` | A parcel is delivered |
| `dhl_parcel_delivery_time_changed` | The expected delivery window changes |
| `dhl_outgoing_parcel_status_changed` | An outgoing parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `dhl_outgoing_parcel_delivered` | An outgoing parcel is delivered |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up. There is no outgoing `registered` or `delivery_time_changed` event.

## Services

| Service | Fields | Description |
|---|---|---|
| `dhl.track_parcel` | `tracking_code`, `config_entry_id` (optional) | Track a parcel that is not already in your account's inbox |
| `dhl.untrack_parcel` | `tracking_code`, `config_entry_id` (optional) | Stop tracking a manually-added parcel |

`config_entry_id` is only needed if you have more than one DHL account set up.

## Examples

Ready-to-paste automations and dashboard snippets live in [`examples/`](examples/), including tracking a new parcel straight from a dashboard.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  default: warning
  logs:
    custom_components.dhl: debug
```

**The diagnostics download is the preferred way to help.** Go to
**Settings → Devices & Services → DHL → ⋮ → Download diagnostics**. It is
already redacted (names, addresses, tracking numbers, tokens) and safe to
attach to a GitHub issue directly — it is the main way this pre-release gets
finished, since nobody building it has a DHL parcel of their own.

## Troubleshooting

- **A parcel shows `unknown`** — DHL has not scanned it yet, or its progress
  value is one this integration has not mapped. Check the logs for a
  ready-to-paste issue link.
- **A parcel seems stuck on "out for delivery"** — this is the known gap for
  Filiale: DHL's progress ladder has no value for "waiting at a Filiale", so
  that arrival may look identical to a parcel still on the delivery vehicle.
  (Packstation arrivals are recognised.) If this happens to you, please
  [open an issue](https://github.com/ha-parcel-integrations/ha-dhl/issues/new)
  or attach a diagnostics export — this is the single most useful thing a
  tester can confirm right now.
- **Sensors go `unavailable` instead of dropping to zero** — when DHL lists
  a parcel but will not hand over its details, the update is failed on purpose
  rather than reported as an empty account. That keeps a temporary glitch from
  looking like "all parcels delivered" and firing your automations. The next
  successful poll restores the sensors. A warning naming the number of
  affected parcels is written to the log.
- **Sign-in fails or the pasted link is rejected** — make sure you copied the
  *entire* address after logging in, including everything after `code=`.
  Trailing characters get trimmed automatically, but a truncated copy will
  not.
- **Re-authentication is requested while everything looked fine** — DHL
  sometimes hands back a refreshed session that is no longer linked to your
  account. It still works as a login, but your parcel list would stay empty,
  so the integration asks you to sign in again rather than showing nothing.
  Signing in restores the link.
- **Re-authentication is requested** — DHL's session expired (they last
  about 30 minutes and are refreshed automatically in the background; this
  only triggers if the refresh itself is rejected). Repeat the sign-in step.
- **A tracking-code Express parcel barely updates** — this is expected, not a
  bug; see [Tracking codes](#tracking-codes) for why. A DHL Parcel barcode
  (`3S…`/`JJD…`/`CR…`/`LX…`) updates on the normal cadence.
- **A tracking code never resolves** — double-check it against the shape
  table in [Tracking codes](#tracking-codes). A code that matches none of
  the known DHL Parcel barcode families and isn't a 10-digit Express AWB is
  still tried on every backend in that table's last row. If none of them
  knows it, it stays `unknown`.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://ha-parcel-integrations.github.io/) for the current list of supported carriers.

## Disclaimer

This is an independent, community-built project. It is not affiliated with, endorsed by, sponsored by, or supported by DHL, Home Assistant, or any other third party referenced in this project. Please don't contact DHL for support with this integration.

All third-party trademarks, trade names, product names, logos, and other brand assets are the property of their respective owners. References to them are solely to identify the relevant carrier or service and do not imply affiliation, sponsorship, or endorsement. Nothing in this project grants or implies any licence or right to use third-party brand assets.

This integration may rely on public, unofficial, or undocumented carrier interfaces, accessed with your own account or API key where required. These may change or be withdrawn without notice and may be subject to DHL's terms. Data is sent only to DHL's own services or those of its group; this project operates no servers of its own. You are responsible for ensuring that your use complies with applicable law and those terms. Use is at your own risk; see the [licence](LICENSE) for warranty limitations.

This integration uses the same account-inbox endpoint the DHL website uses once you are logged in. Your credentials never pass through this integration or any third party — sign-in happens directly in your own browser against DHL's own login page; only the resulting refresh token is stored in Home Assistant's own config-entry storage, the same way any other integration's credentials are.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
