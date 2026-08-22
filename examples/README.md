# Examples

Ready-to-paste Home Assistant snippets for the DHL integration.

| Folder | Contents |
|---|---|
| [`automations/`](automations/) | YAML automations — copy them into your `automations.yaml` or paste them into the Automation editor in **raw editor** mode. |
| [`dashboards/`](dashboards/) | Lovelace card snippets, e.g. tracking a parcel by number straight from a dashboard. |

Parcels addressed to your account show up automatically — there is nothing
to register by hand for those. Use the [`dhl.track_parcel`](dashboards/add_parcel_card.yaml)
service only for a parcel your account's own inbox would not otherwise
carry (a parcel someone else is sending you, for example).

All examples assume a single DHL account. Adjust entity IDs to match
yours; with more than one account configured, every entity ID carries the
account name, and `dhl.track_parcel`/`dhl.untrack_parcel` need `config_entry_id`.

## Events used in the examples

The coordinator fires these on the HA event bus:

| Event | When | Payload |
|---|---|---|
| `dhl_parcel_registered` | A new parcel appears in the active list | The full normalised parcel dict |
| `dhl_parcel_status_changed` | A parcel's canonical status changes | Same, plus `old_status` / `new_status` |
| `dhl_parcel_delivered` | A parcel reaches the delivered status | Same (fires *instead of* `status_changed` on that final hop) |
| `dhl_parcel_delivery_time_changed` | A parcel's expected delivery time changes | Same, plus `old_planned_from` / `new_planned_from` / `old_planned_to` / `new_planned_to` |

Every payload also carries the account's `device_id`, which is what device
triggers filter on. Events are suppressed on the first refresh after start-up.
