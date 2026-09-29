# inwendo ERP / vynst Integration for Home Assistant

A Home Assistant custom integration that syncs booking calendars from the inwendo ERP / vynst system and, if you opt in, connects smart locks in both directions.

## Features

- Automatically discovers bookable resources from your ERP instance
- Creates calendar entities for each bookable resource
- Supports webhook-based instant refresh when bookings change
- Polls for updates every 15 minutes as a fallback
- Optional: ERP smart locks (Nuki, LOQED, …) as Home Assistant lock entities
- Optional: offer Home Assistant locks to the ERP, so ERP users can open them with the ERP's access rules

## Requirements

- Home Assistant 2025.1 or newer (tested with 2026.9)
- An ERP version with the `/api/homeassistant/*` endpoints; the smart lock features need the `smart_locks` / `ha_locks` endpoints

## Installation

### HACS (Recommended)

1. Open HACS in your Home Assistant instance
2. Go to **Integrations**
3. Click the three-dot menu in the top right and select **Custom repositories**
4. Enter the repository URL: `https://github.com/inwendo/iw_erp_homeassistant`
5. Select **Integration** as the category
6. Click **Add**
7. Search for "inwendo ERP" and click **Install**
8. Restart Home Assistant

### Manual

1. Copy the `custom_components/iw_erp_homeassistant` directory to your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings** > **Devices & Services** > **Add Integration**
2. Search for "inwendo ERP"
3. Enter your ERP host URL (e.g., `https://your-erp-instance.example.com`)
4. Enter your API Key (JWT)

If the ERP later rejects the API key (expired or revoked), Home Assistant asks for a new one (**Re-authenticate**).

### API Key

An API key can be created in the inwendo ERP UI under the user settings. The API key needs the following scopes:

- **Location** (read) - for listing bookable resources
- **Event Booking** (read + write) - for calendar data and webhook registration
- **Event** (write) - only for *Offer Home Assistant locks to the ERP*

For additional security, the API key can be restricted to the path `/api/homeassistant/.*` so it only has access to the endpoints needed by this integration. The trailing `.*` makes it a prefix; `/api/homeassistant/*` would match nothing.

## Webhook

The integration automatically registers a webhook with your ERP server during setup. When bookings change in the ERP, the server pushes an update to Home Assistant for instant calendar refresh.

The ERP only accepts webhook URLs with `https://` and a public host name, so Home Assistant needs an external URL (**Settings > System > Network**, or Home Assistant Cloud). Without one the integration keeps polling.

## Smart locks (opt-in)

Both directions are off by default. Enable them under **Settings > Devices & Services > inwendo ERP > Configure**.

### Import ERP smart locks

Adds a lock entity for every ERP smart lock the API key's user may use (the same rules as in the ERP apps: booking, self-service, employment contract, or super admin). Locking and opening go through the ERP, which checks the access rights and writes its smart lock log. The state is polled every 10 minutes and shortly after every command.

### Offer Home Assistant locks to the ERP

Select the Home Assistant locks the ERP may use. The integration sends them and every state change to the ERP, which shows them as a smart lock connection named after your Home Assistant. **Nothing happens in the ERP until an administrator syncs the devices of that connection** and assigns the new smart locks to bookables, users or contracts – so both sides opt in.

When an ERP user opens such a lock, the ERP sends a signed command to this integration's webhook:

- signature: HMAC-SHA256 of the request body with a secret that only this installation and the ERP know (header `X-IW-Signature`)
- commands older than two minutes or replayed are rejected
- only the locks you selected are accepted; ERP locks imported into Home Assistant can never be offered back

This needs the external `https://` URL described under *Webhook*. Deselecting all locks (or removing the integration) withdraws the offer in the ERP; the ERP keeps the smart locks and re-activates them when you offer the locks again.

## Development

```bash
pip install -r requirements_test.txt   # Python 3.14 for Home Assistant 2026.9
pytest                                 # integration tests with a mocked ERP
IW_E2E_ERP_HOST=http://127.0.0.1:8000 IW_E2E_ERP_TOKEN=<jwt> pytest -m live   # against a real ERP
```

The live tests need a token with the scopes Location, Event Booking and Event and "Allow super admin functions" (they create their own test data). The ERP repository contains the matching API contract tests (`e2e/tests/api/homeassistant-plugin.spec.ts`).
