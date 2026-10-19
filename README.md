# Amazon Shipping Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-amazon-shipping.svg)](https://github.com/ha-parcel-integrations/ha-amazon-shipping/releases)
[![Downloads](https://img.shields.io/github/downloads/ha-parcel-integrations/ha-amazon-shipping/total.svg)](https://github.com/ha-parcel-integrations/ha-amazon-shipping/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 📦 Part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/), a family of package-tracking integrations for Home Assistant that all publish the same parcel format, statuses and events, so they plug into cross-carrier automations and the optional [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator). Browse the other carriers on the website.
>
> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks [Amazon Shipping](https://track.amazon.com) parcels, the shipments sellers and merchants send through Amazon Shipping and Multi-Channel Fulfillment, in the United States, the United Kingdom, France, Italy and Spain. No account is needed — you enter the tracking code yourself, just like on Amazon's tracking page.

This is not an Amazon account integration: it does not read your Amazon orders. A tracking code that belongs to a normal Amazon order is not served by this tracker and shows `unknown`.

> **Pre-1.0.** Built from eight delivered or in-flight parcels. Failed delivery attempts, returns, pickup points and customs have not been seen yet, so those statuses are recognised by their wording and the integration logs one warning when it relies on that. If a status cannot be mapped at all the parcel shows `unknown` and one warning is logged. A warning names the kind of code and the code itself, never any personal data. Please [open an issue](https://github.com/ha-parcel-integrations/ha-amazon-shipping/issues/new?template=unrecognised_status.yml) when you see it.

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

- Track any number of Amazon Shipping parcels by tracking code — no account needed
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `out_for_delivery` / `delivered` / …), the carrier's own status code, the expected delivery moment and a tracking deep-link
- Summary sensors: incoming parcels, next delivery, recently delivered parcels
- Read-only **Deliveries** calendar with the expected delivery windows
- `amazon_shipping.track_parcel` / `amazon_shipping.untrack_parcel` services, so a dashboard button can add a parcel
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- Home Assistant 2025.12 or newer
- An Amazon Shipping or Multi-Channel Fulfillment parcel and its tracking code
  (from the shipping confirmation or the seller's message), 8 to 40 letters and
  digits — no account needed. Codes such as `TBA…`, `UK…` and long numeric
  codes are all accepted; whether the parcel is served is decided by Amazon's
  tracker, not by the shape of the code.

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-amazon-shipping` as an **Integration**.
3. Install **Amazon Shipping** and restart Home Assistant.

### Manual

Copy `custom_components/amazon_shipping` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → Amazon Shipping**. There is nothing to fill in: the hub is created immediately (Amazon Shipping tracking needs no account).

Then add parcels via the integration's **Configure** dialog, the [`amazon_shipping.track_parcel`](#services) service, or a [dashboard button](examples/dashboards/add_parcel_card.yaml). Spaces, dots and dashes in a pasted code are ignored.

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Parcels | Add / remove | — | Manage the tracked tracking codes. Changes apply immediately, no restart. |
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with each status update. |

Polling isn't one of these settings: the integration polls on a dynamic,
status-driven schedule with nothing to configure.

## Dynamic polling

Polling isn't a setting here — the integration adjusts its own cadence to
what your tracked parcels are actually doing:

- **Quiet hours** — no polling between 00:00–06:00 local time, aside from one
  catch-up check at each end of that window (around midnight and around 6
  AM), so an overnight update is never missed.
- **Hot (every 15 minutes)** — while any tracked parcel is out for delivery
  today, starting an hour before its delivery window opens (or immediately if
  no window is known yet).
- **Normal (every 45 minutes)** — for anything else still on its way.
- **Fully paused** — once every tracked parcel has been delivered, or nothing
  is tracked at all, polling stops until you add a parcel back (adding one
  always triggers an immediate check, regardless of the pause).
- A small, fixed per-hub offset is added on top, so not every Amazon Shipping
  hub out there polls at exactly the same second.

Amazon reports a single expected delivery moment rather than a window, and it is
not always present. A parcel that is out for delivery without one is polled at
the hot cadence. A parcel whose history Amazon no longer keeps is left alone: it
is not polled again, and polling stops once nothing else is waiting.

## Removal

Standard HA removal applies: **Settings → Devices & Services → Amazon Shipping → ⋮ → Delete**. Nothing is stored on Amazon Shipping's side.

## Sensors

| Entity | Description |
|---|---|
| `sensor.amazon_shipping_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.amazon_shipping_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.amazon_shipping_next_delivery` | Earliest expected delivery moment across all active parcels |
| `sensor.amazon_shipping_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `sensor.amazon_shipping_last_successful_update` | Diagnostic: when Amazon Shipping was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family:

| Status | Meaning |
|---|---|
| `registered` | Label created, nothing handed over yet |
| `in_transit` | Picked up or moving through Amazon's network, including delayed parcels |
| `out_for_delivery` | With the courier today |
| `at_pickup_point` | Waiting for collection (not yet seen) |
| `delivered` | Delivered |
| `returning` | Returned or going back to the sender (not yet seen) |
| `problem` | A failed attempt or other exception (not yet seen) |
| `unknown` | Not served by the tracker, history no longer kept, or a status we have not mapped yet |

The carrier's own status code is always available as `raw_status`.

## Events

The integration fires these on the event bus (also available as device triggers on the Amazon Shipping device):

| Event | When |
|---|---|
| `amazon_shipping_parcel_registered` | A new parcel appears in the active list |
| `amazon_shipping_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `amazon_shipping_parcel_delivered` | A parcel is delivered |
| `amazon_shipping_parcel_delivery_time_changed` | The expected delivery window changes |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up.

## Services

| Service | Fields | Description |
|---|---|---|
| `amazon_shipping.track_parcel` | `tracking_code` | Start tracking a parcel |
| `amazon_shipping.untrack_parcel` | `tracking_code` | Stop tracking a parcel |

## Examples

Ready-to-paste automations and dashboard snippets live in [`examples/`](examples/), including tracking a new parcel straight from a dashboard.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.amazon_shipping: debug
```

## Troubleshooting

- **A parcel shows `unknown`** — Amazon's tracker does not serve that code (a mistyped code, a normal Amazon order, a parcel not yet scanned, or one whose history Amazon no longer keeps). It will pick up automatically once the tracker serves it, except for an expired history, which is not polled again.
- **A status logs "Unrecognised Amazon Shipping data"** — please [open an issue](https://github.com/ha-parcel-integrations/ha-amazon-shipping/issues/new?template=unrecognised_status.yml) with the logged line so the mapping can be extended.
- **Times look an hour off** — Amazon sends times without a time zone. UK codes are read as London time; for every other code the time is shown as Amazon wrote it.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://ha-parcel-integrations.github.io/) for the current list of supported carriers.

## Disclaimer

This is an independent, community-built project. It is not affiliated with, endorsed by, sponsored by, or supported by Amazon Shipping, Home Assistant, or any other third party referenced in this project. Please don't contact Amazon Shipping for support with this integration.

All third-party trademarks, trade names, product names, logos, and other brand assets are the property of their respective owners. References to them are solely to identify the relevant carrier or service and do not imply affiliation, sponsorship, or endorsement. Nothing in this project grants or implies any licence or right to use third-party brand assets.

This integration uses the public, unofficial tracking endpoint behind Amazon's own tracking page, which may change or be withdrawn without notice and may be subject to Amazon's terms. Data is sent only to Amazon's tracking service; this project operates no servers of its own. You are responsible for ensuring that your use complies with applicable law and those terms. Use is at your own risk; see the [licence](LICENSE) for warranty limitations.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
