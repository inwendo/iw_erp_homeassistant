# vynst (inwendo ERP) for Home Assistant

[Deutsch](README.de.md) | English

A Home Assistant integration that syncs booking calendars from vynst (inwendo ERP) and, if you opt in, connects smart locks in both directions, adds occupancy sensors, access and check-in events, usage counters and room display images.

## Features

- Automatically discovers bookable resources from your vynst (inwendo ERP) instance
- Creates calendar entities for each bookable resource
- Supports webhook-based instant refresh when bookings change
- Polls for updates every 15 minutes as a fallback
- Optional: vynst (inwendo ERP) smart locks (Nuki, LOQED, …) as Home Assistant lock entities
- Optional: offer Home Assistant locks to vynst (inwendo ERP), so its users can open them with the access rules of vynst (inwendo ERP)
- Optional: show vynst (inwendo ERP) room displays (door signs) on [OpenDisplay](https://opendisplay.org) e-paper panels, with battery and offline reports back to vynst (inwendo ERP), and as image entities on dashboards
- Optional: occupancy sensors per bookable (occupied, pre-start lead time, open, next booking, participants)
- Optional: `iw_erp_access` / `iw_erp_check_in` events, device triggers, "last access" sensors and logbook entries
- Optional: vynst (inwendo ERP) usage counters as sensors, and Home Assistant meters reported to them
- Services `iw_erp_homeassistant.check_access` (card / QR / keypad readers with the access rules of vynst (inwendo ERP)) and `iw_erp_homeassistant.create_ticket`
- Blueprints: preheat a room for its bookings, light for bookings, frost protection, heating and light by room occupancy
- Repairs (no external URL, rejected API key, missing scope) and diagnostics download

## Requirements

- Home Assistant 2025.1 or newer (tested with 2026.9)
- A vynst (inwendo ERP) version with the `/api/homeassistant/*` endpoints; the smart lock features need the `smart_locks` / `ha_locks` endpoints
- The functions added in 1.4 need a vynst (inwendo ERP) version that offers them **and** has them switched on in its settings (see [vynst (inwendo ERP) settings](#erp-settings-feature-flags)). When a function is off in vynst (inwendo ERP), the integration logs one info line and leaves it out – no errors, no re-authentication.

## Installation

### Home Assistant Community Store (HACS, recommended)

1. Open **HACS** in Home Assistant
2. Search for **vynst** and open **vynst (inwendo ERP)**
3. Click **Download**, then restart Home Assistant

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=inwendo&repository=iw_erp_homeassistant&category=integration)

Updates show up in HACS like those of every other integration. The versions are the [GitHub releases](https://github.com/inwendo/iw_erp_homeassistant/releases) of this repository.

### Manual

1. Copy the `custom_components/iw_erp_homeassistant` directory to your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings** > **Devices & Services** > **Add Integration**
2. Search for "vynst"
3. Enter the host URL of vynst (inwendo ERP) (e.g., `https://your-erp-instance.example.com`)
4. Enter your API Key (JWT)

If vynst (inwendo ERP) later rejects the API key (expired or revoked), Home Assistant asks for a new one (**Re-authenticate**).

### API Key

An API key can be created in the vynst (inwendo ERP) UI under the user settings. The API key needs the following scopes:

| Scope | Needed for |
|---|---|
| **Location** (read) | bookable resources, occupancy sensors, room displays, reading usage counters |
| **Location** (write) | *Usage counters*: reporting Home Assistant meters |
| **Event Booking** (read + write) | calendar data and webhook registration (also the access and check-in events) |
| **Event** (write) | *Offer Home Assistant locks to vynst (inwendo ERP)*, service `check_access` |
| **Ticket** (write) | service `create_ticket` |

A missing scope for a function you switched on shows up under **Settings > System > Repairs** (HTTP 403), not as a re-authentication.

For additional security, the API key can be restricted to the path `/api/homeassistant/.*` so it only has access to the endpoints needed by this integration. The trailing `.*` makes it a prefix; `/api/homeassistant/*` would match nothing.

## Webhook

The integration automatically registers a webhook with vynst (inwendo ERP) during setup. When bookings change in vynst (inwendo ERP), the server pushes an update to Home Assistant for instant calendar refresh.

vynst (inwendo ERP) only accepts webhook URLs with `https://` and a public host name, so Home Assistant needs an external URL (**Settings > System > Network**, or Home Assistant Cloud). Without one the integration keeps polling, and a repair issue says so; it disappears once the webhook could be registered.

The registration also sends a `webhook_token`. A vynst (inwendo ERP) version that echoes it as `X-IW-Webhook-Token` lets the integration mark access and check-in events as `verified: true` and reject requests with a wrong token; without the header, events are still fired with `verified: false` (the webhook URL is not a secret – check `verified` before an automation does something security relevant).

Webhook payloads are dispatched by their class in vynst (inwendo ERP): bookings patch the calendars (and refresh the occupancy), smart lock log entries and attendance list entries become events (see below), anything else is ignored.

## Options

All functions below are off by default. Switch them on under **Settings > Devices & Services > vynst (inwendo ERP) > Configure**:

| Option | What it adds |
|---|---|
| Import vynst (inwendo ERP) smart locks | lock entities for vynst (inwendo ERP) smart locks |
| Offer these Home Assistant locks to vynst (inwendo ERP) | vynst (inwendo ERP) can open the selected locks |
| Occupancy sensors | sensors per bookable |
| Access and check-in events | `iw_erp_access` / `iw_erp_check_in` events, last access sensors, device triggers, logbook |
| Usage counters | vynst (inwendo ERP) usage counters as sensors; a second page maps Home Assistant meters to them |
| Room display images | image entities for all vynst (inwendo ERP) room displays (displays assigned to an OpenDisplay panel always get one) |
| Room displays (page shown when the OpenDisplay integration is installed) | assign OpenDisplay panels |

### vynst (inwendo ERP) settings (feature flags)

| vynst (inwendo ERP) setting | Function |
|---|---|
| `integration_occupancy_api_enabled` | occupancy sensors |
| `integration_ha_access_events_enabled` | access and check-in events |
| `integration_ha_access_check_enabled` | service `check_access` |
| `integration_ha_ticket_enabled` | service `create_ticket` |
| `integration_usage_counter_api_enabled` | usage counters |

## Occupancy sensors (opt-in)

For every bookable vynst (inwendo ERP) lists in `/api/homeassistant/occupancy` (polled every minute, and right after every booking webhook):

| Entity | State | Attributes |
|---|---|---|
| `binary_sensor.<room>_occupied` | a booking runs now | booking_id, title, start, end, participants, post_end_active |
| `binary_sensor.<room>_pre_start` | now within the lead time before the next booking | pre_start_minutes, next_title, next_start, ... |
| `binary_sensor.<room>_open` | open now (only for bookables with opening hours) | next_open, next_close |
| `sensor.<room>_next_booking` | start of the next booking (14 days ahead) | title, participants, start, end |
| `sensor.<room>_participants` | participants of the running booking | title, start, end |

The lead time is the bookable's `automation_pre_start_minutes` in vynst (inwendo ERP) (else the default of vynst (inwendo ERP), 30 minutes). Each bookable is a device; its calendar entity belongs to the same device.

The entity ids follow the language of Home Assistant: a German Home Assistant names them `binary_sensor.<raum>_belegt`, `binary_sensor.<raum>_vorlauf`, `sensor.<raum>_nachste_buchung` and so on. Pick them in the entity picker rather than typing the English ids.

## Access and check-in events (opt-in)

With the option on, the webhook registration asks vynst (inwendo ERP) for `"events": ["access", "check_in"]`; vynst (inwendo ERP) then posts every smart lock log entry and every check-in / check-out. They become events on the Home Assistant bus (bookings are no events – use the calendar trigger with an offset for those):

`iw_erp_access`: `smart_lock_id`, `smart_lock_name`, `action` (`unlock`, `lock`, `access_code`, `access_denied`), `success`, `person_id`, `person_name`, `source` (`app`, `homeassistant`, `nodered`, ...), `at`, `config_entry_id`, `erp_entity_id`, `verified`

`iw_erp_check_in`: `attendance_list_id`, `attendance_list_name`, `person_id`, `person_name`, `checked_in` (false on check-out), `at`, `config_entry_id`, `erp_entity_id`, `verified`

Also added: a **Last access** sensor on the device "vynst (inwendo ERP) <host>" (and one per imported vynst (inwendo ERP) lock when *Import vynst (inwendo ERP) smart locks* is on), logbook entries, and device triggers on that device: *Smart lock access granted / denied*, *Person checked in / out*.

```yaml
# Notify when someone is denied at the main entrance
triggers:
  - trigger: event
    event_type: iw_erp_access
    event_data:
      smart_lock_name: Haupteingang
      success: false
actions:
  - action: notify.mobile_app_phone
    data:
      message: "Access denied for {{ trigger.event.data.person_name or 'unknown' }}"
```

```yaml
# Gym lights on with the first check-in
triggers:
  - trigger: event
    event_type: iw_erp_check_in
    event_data:
      attendance_list_name: Fitnessstudio
      checked_in: true
actions:
  - action: light.turn_on
    target:
      entity_id: light.gym
```

## Services

### `iw_erp_homeassistant.check_access`

Asks vynst (inwendo ERP) whether a credential opens a smart lock – for RFID, QR or keypad readers in Home Assistant. Fields: `credential`, `credential_type` (`auto`, `access_code`, `member_card`, `card`), `smart_lock_id` **or** `lock` (an imported vynst (inwendo ERP) lock, or a Home Assistant lock offered to vynst (inwendo ERP)), `open` (vynst (inwendo ERP) opens a lock it controls itself), `config_entry_id` (only with several vynst (inwendo ERP) instances). vynst (inwendo ERP) writes its smart lock log (source `homeassistant`) and the service returns its answer:

```yaml
triggers:
  - trigger: event
    event_type: esphome.rfid_scanned   # your reader
actions:
  - action: iw_erp_homeassistant.check_access
    data:
      credential: "{{ trigger.event.data.uid }}"
      credential_type: card
      lock: lock.front_door
    response_variable: access
  - if: "{{ access.granted }}"
    then:
      - action: lock.open
        target:
          entity_id: lock.front_door
```

The answer: `granted`, `reason` (`granted`, `unknown_credential`, `no_access`, `invalid_code`, `code_expired`), `credential_type`, `smart_lock`, `person`, `opened`. Needs the scope **Event** (write).

### `iw_erp_homeassistant.create_ticket`

Creates a ticket in vynst (inwendo ERP): `title`, `description`, `bookable` (a bookable device of this integration) or `bookable_id`, `priority` (`low`, `normal`, `high`, `urgent`), `dedupe_key`. With a `dedupe_key` vynst (inwendo ERP) creates no second ticket while one with that key is open (answer `{"id": 88, "status": "already_open"}`). Needs the scope **Ticket** (write).

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.hall_1_water_leak
    to: "on"
actions:
  - action: iw_erp_homeassistant.create_ticket
    data:
      title: Wasseralarm Halle 1
      description: The leak sensor under the sink reports water.
      bookable_id: 7
      priority: urgent
      dedupe_key: water_leak_hall_1
```

Both services always exist; when the function is switched off in vynst (inwendo ERP) they fail with a clear message.

## Usage counters (opt-in)

Every vynst (inwendo ERP) usage counter (`/api/homeassistant/usage_counters`) becomes a read-only sensor on its bookable's device. On the second page of the options, pick the Home Assistant sensor that feeds a counter: its readings are sent to vynst (inwendo ERP) when they change – only numbers, in the counter's unit, at most every 15 minutes per counter (the latest reading wins). Counters vynst (inwendo ERP) fills from real usage data are not offered.

## Blueprints

Automation blueprints built on the calendar trigger with offsets – they work with the calendar entities alone:

| Blueprint | Import |
|---|---|
| Preheat a room before each booking, set back to eco after the end | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Finwendo%2Fiw_erp_homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fiw_erp%2Fpreheat_room.yaml) |
| Light on at the booking start, off N minutes after the end | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Finwendo%2Fiw_erp_homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fiw_erp%2Fbooking_light.yaml) |
| Frost protection outside bookings and opening hours | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Finwendo%2Fiw_erp_homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fiw_erp%2Ffrost_protection.yaml) |
| Heat a room by its occupancy sensors (comfort before and during bookings, eco after) | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Finwendo%2Fiw_erp_homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fiw_erp%2Foccupancy_heating.yaml) |
| Light by its occupancy sensors (on while occupied, off N minutes after the end) | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Finwendo%2Fiw_erp_homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fiw_erp%2Foccupancy_light.yaml) |

Or **Settings > Automations & Scenes > Blueprints > Import blueprint** with the file's GitHub URL. Lead times are durations relative to the booking start; a negative value means before the start (the preheat default is -30 minutes). After the end, the automations check the calendar first: a booking running or starting soon keeps the room warm / lit.

The calendar trigger of Home Assistant only sees events that existed about 15 minutes before their trigger time, so a booking made at short notice (or cancelled shortly before) can be missed by the calendar based blueprints. The two occupancy blueprints use the occupancy sensors instead (option **Occupancy sensors**, vynst (inwendo ERP) setting `integration_occupancy_api_enabled`); they follow every booking change within a minute.

## Smart locks (opt-in)

Both directions are off by default. Enable them under **Settings > Devices & Services > vynst (inwendo ERP) > Configure**.

### Import vynst (inwendo ERP) smart locks

Adds a lock entity for every vynst (inwendo ERP) smart lock the API key's user may use (the same rules as in the vynst (inwendo ERP) apps: booking, self-service, employment contract, or super admin). Locking and opening go through vynst (inwendo ERP), which checks the access rights and writes its smart lock log. The state is polled every 10 minutes and shortly after every command.

### Offer Home Assistant locks to vynst (inwendo ERP)

Select the Home Assistant locks vynst (inwendo ERP) may use. The integration sends them and every state change to vynst (inwendo ERP), which shows them as a smart lock connection named after your Home Assistant. **Nothing happens in vynst (inwendo ERP) until an administrator syncs the devices of that connection** and assigns the new smart locks to bookables, users or contracts – so both sides opt in.

When a vynst (inwendo ERP) user opens such a lock, vynst (inwendo ERP) sends a signed command to this integration's webhook:

- signature: HMAC-SHA256 of the request body with a secret that only this installation and vynst (inwendo ERP) know (header `X-IW-Signature`)
- commands older than two minutes or replayed are rejected
- only the locks you selected are accepted; vynst (inwendo ERP) locks imported into Home Assistant can never be offered back

This needs the external `https://` URL described under *Webhook*. Deselecting all locks (or removing the integration) withdraws the offer in vynst (inwendo ERP); vynst (inwendo ERP) keeps the smart locks and re-activates them when you offer the locks again.

## Room displays on OpenDisplay panels (opt-in)

[OpenDisplay](https://opendisplay.org) panels are e-paper screens that are pushed to over Bluetooth by a sender next to them. With the [OpenDisplay integration](https://github.com/OpenDisplay/Home_Assistant_Integration) installed, Home Assistant is that sender, and this integration brings the picture from vynst (inwendo ERP).

1. In vynst (inwendo ERP), create a room display (**Räume & Ressourcen > Raum-Displays**) of the type **OpenDisplay** on a room or a location, and set the panel's width and height (default 800 x 480).
2. Pair the panel with the OpenDisplay integration in Home Assistant.
3. Under **Settings > Devices & Services > vynst (inwendo ERP) > Configure**, the second page lists the OpenDisplay room displays of vynst (inwendo ERP): pick the panel for each one.

For every assigned display Home Assistant then

- fetches the picture from vynst (inwendo ERP) (`/api/homeassistant/displays/{id}/frame.png`, with the ETag, so an unchanged sign costs no Bluetooth upload),
- uploads it with `opendisplay.upload_image` from the local media folder (`media/iw_erp_displays/`); dithering to the panel's colours happens there,
- reports battery voltage, signal, firmware and the panel's last contact to vynst (inwendo ERP), which mails the room's administration when the battery runs low or the panel stops responding,
- asks again when vynst (inwendo ERP) says the picture changes next: the configured interval, or earlier when a booking starts or ends.

Every assigned display also gets an image entity with the current frame (e.g. for a picture card on a dashboard). With the option **Room display images**, all vynst (inwendo ERP) room displays get one, also those without a panel; they are fetched the same way (ETag, `X-Refresh-After`), just not uploaded.

vynst (inwendo ERP) draws everything (room, bookings, booking QR code); Home Assistant needs no configuration beyond the assignment. Home Assistant needs a local media folder (the default `media/` of Home Assistant OS works). The OpenDisplay "Last seen" sensor is disabled by default; when it is off, the last battery or signal reading counts as the panel's last contact.

## Repairs and diagnostics

Under **Settings > System > Repairs** the integration reports, and clears again once fixed:

- vynst (inwendo ERP) cannot push changes because Home Assistant has no external https URL (polling instead),
- vynst (inwendo ERP) rejected the API key (besides the re-authentication),
- the API key lacks the scope for a function you switched on (HTTP 403).

**Download diagnostics** (on the integration's menu) contains the options, the state of every function and the last vynst (inwendo ERP) answers; the API key, secrets, the webhook token, credentials in the host URL, booking titles and person names are redacted.

## Development

```bash
pip install -r requirements_test.txt   # Python 3.14 for Home Assistant 2026.9
pytest                                 # integration tests with a mocked ERP
IW_E2E_ERP_HOST=http://127.0.0.1:8000 IW_E2E_ERP_TOKEN=<jwt> pytest -m live   # against a real ERP
```

The live tests need a token with the scopes Location, Event Booking and Event (and Ticket for the ticket test) and "Allow super admin functions" (they create their own test data). Tests of functions behind vynst (inwendo ERP) settings check the "switched off" behaviour when the setting is off. vynst (inwendo ERP) repository contains the matching API contract tests (`e2e/tests/api/homeassistant-plugin.spec.ts`).
