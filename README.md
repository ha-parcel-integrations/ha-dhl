# DHL Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-dhl.svg)](https://github.com/ha-parcel-integrations/ha-dhl/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your **DHL Paket** (Germany)
parcels. Log in once with your DHL Kundenkonto and your parcels are imported
automatically — no tracking codes to copy in by hand. You can also add a
parcel that is not in your account's own inbox by its tracking number.

Part of the [ha-parcel-integrations](https://github.com/ha-parcel-integrations) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

**Country support:** Germany only, today. Country requests go through the
[organisation discussion](https://github.com/ha-parcel-integrations/.github/discussions/new/choose).
DHL's Netherlands business is a separate integration,
[**ha-dhl-nl**](https://github.com/ha-parcel-integrations/ha-dhl-nl).

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
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
- Summary sensors: incoming parcels, next delivery, recently delivered parcels
- Read-only **Deliveries** calendar with the expected delivery windows
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor
- Structure-preserving diagnostics export — the tool this pre-release is finished with (see [Debugging](#debugging))

## Requirements

- Home Assistant 2024.7 or newer
- A DHL Kundenkonto (a free DHL account) — the same account you use on
  dhl.de or in the DHL Paket app
- A browser to complete the one-time sign-in during setup
- **A German IP address.** DHL's tracking endpoint only answers requests
  that originate from Germany — this is normally not a concern (a DHL Paket
  customer's own Home Assistant is already reached from Germany), but it
  means the integration will not work over a non-German VPN, from a
  non-German cloud/VPS-hosted Home Assistant, or during development/testing
  from outside Germany.

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-dhl` as an **Integration**.
3. Install **DHL** and restart Home Assistant.

### Manual

Copy `custom_components/dhl` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

1. Go to **Settings → Devices & Services → Add Integration → DHL**.
2. Pick a country — only Germany is supported today.
3. The next form shows a sign-in link. Open it in a browser and log in with
   your DHL Kundenkonto.
4. Your browser will fail to open the final `dhllogin://…` redirect it lands
   on — **that is expected**. This address never appears in the address bar
   (no desktop browser has an app registered for it); you catch it in your
   browser's developer tools' Network tab instead. See
   [docs/finding-the-redirect-url.md](docs/finding-the-redirect-url.md) for
   step-by-step instructions for Chrome, Edge, Firefox and Safari. Paste the
   full address into the form.
5. Submit. Your account's parcels start appearing on the next poll.

Nothing is typed into Home Assistant itself except that pasted-back address —
your DHL password never passes through this integration.

### Adding a parcel that is not in your account

Your DHL account inbox only shows parcels addressed to you. To also track a
parcel someone else is sending you (or one your account has not picked up
yet), call the [`dhl.track_parcel`](#services) service with its tracking
number, or use a [dashboard button](examples/dashboards/add_parcel_card.yaml).

## Options

Click **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with each status update. |
| Polling | Refresh every | 30 min | How often DHL is checked. Slower is gentler on DHL's API. |

## Removal

Standard HA removal applies: **Settings → Devices & Services → DHL → ⋮ → Delete**. Nothing is stored on DHL's side beyond the normal session your account already has.

## Sensors

| Entity | Description |
|---|---|
| `sensor.dhl_<account>_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.dhl_<account>_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.dhl_<account>_next_delivery` | Earliest expected delivery moment across all active parcels |
| `sensor.dhl_<account>_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `sensor.dhl_<account>_last_successful_update` | Diagnostic: when DHL was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family. DHL Germany reports a coarse 0-5 progress ladder rather than a status vocabulary, so the mapping below is what that ladder can express — it cannot currently distinguish a Packstation/Filiale arrival from an ordinary "out for delivery" (see [Troubleshooting](#troubleshooting)).

| Status | Meaning |
|---|---|
| `registered` | Label created / picked up by DHL |
| `in_transit` | In DHL's network |
| `out_for_delivery` | On a delivery vehicle today |
| `at_pickup_point` | Not currently distinguishable — see Troubleshooting |
| `delivered` | Delivered |
| `returning` | DHL reports the shipment as a return |
| `problem` | Not currently distinguishable — see Troubleshooting |
| `unknown` | A progress value we have not mapped yet |

The carrier's own human-readable text is always available as `raw_status`.

## Events

The integration fires these on the event bus (also available as device triggers on the DHL device):

| Event | When |
|---|---|
| `dhl_parcel_registered` | A new parcel appears in the active list |
| `dhl_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `dhl_parcel_delivered` | A parcel is delivered |
| `dhl_parcel_delivery_time_changed` | The expected delivery window changes |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up.

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
- **A parcel seems stuck on "out for delivery"** — this is the known gap:
  DHL's progress ladder has no value for "waiting at a Packstation/Filiale",
  so a pickup-point arrival may look identical to a parcel still on the
  delivery vehicle. If this happens to you, please
  [open an issue](https://github.com/ha-parcel-integrations/ha-dhl/issues/new)
  or attach a diagnostics export — this is the single most useful thing a
  tester can confirm right now.
- **Sign-in fails or the pasted link is rejected** — make sure you copied the
  *entire* address after logging in, including everything after `code=`.
  Trailing characters get trimmed automatically, but a truncated copy will
  not.
- **Re-authentication is requested** — DHL's session expired (they last
  about 30 minutes and are refreshed automatically in the background; this
  only triggers if the refresh itself is rejected). Repeat the sign-in step.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://github.com/ha-parcel-integrations) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://github.com/ha-parcel-integrations) for the current list of supported carriers.

## Disclaimer

This integration uses the same account-inbox endpoint the DHL website uses once you are logged in. It is not affiliated with, endorsed by, or supported by DHL. Your credentials never pass through this integration or any third party — sign-in happens directly in your own browser against DHL's own login page; only the resulting refresh token is stored in Home Assistant's own config-entry storage, the same way any other integration's credentials are.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
