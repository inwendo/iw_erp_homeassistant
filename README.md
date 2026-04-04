# inwendo ERP / vynst Integration for Home Assistant

A Home Assistant custom integration that syncs booking calendars from the inwendo ERP / vynst system.

## Features

- Automatically discovers bookable resources from your ERP instance
- Creates calendar entities for each bookable resource
- Supports webhook-based instant refresh when bookings change
- Polls for updates every 15 minutes as a fallback

## Installation

### HACS (Recommended)

1. Open HACS in your Home Assistant instance
2. Click on "Integrations"
3. Click the "+" button
4. Search for "inwendo ERP"
5. Click "Install"
6. Restart Home Assistant

### Manual

1. Copy the `custom_components/iw_erp_homeassistant` directory to your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings** > **Devices & Services** > **Add Integration**
2. Search for "inwendo ERP"
3. Enter your ERP host URL (e.g., `https://your-erp-instance.example.com`)
4. Enter your API Key (JWT)

### Generating an API Key

An API key can be generated on the server using:

```bash
php app/console inwendo:login:add:apikey:user --user=<username>
```

Use the JWT token value from the output as your API key.

## Webhook

The integration registers a webhook at `/api/webhook/iw_erp_homeassistant`.
Your ERP system can send a POST request with a JSON body containing `{"id": <bookable_id>}` to trigger an immediate calendar refresh for that resource.
