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

### API Key

An API key can be created in the inwendo ERP UI under the user settings. The API key requires the **Event** scope with read access.

For additional security, the API key can be restricted to the `/api/homeassistant/*` path so it only has access to the endpoints needed by this integration.

## Webhook

The integration automatically registers a webhook with your ERP server during setup. When bookings change in the ERP, the server pushes an update to Home Assistant for instant calendar refresh.

No manual webhook configuration is required.
