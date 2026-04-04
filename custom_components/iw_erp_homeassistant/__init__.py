"""The inwendo ERP / vynst integration."""
import logging
from aiohttp import web

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Define the platforms we want to set up
PLATFORMS = ["calendar"]

# This is our universal webhook ID. The URL will be /api/webhook/iw_erp_homeassistant
UNIVERSAL_WEBHOOK_ID = DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ERP Calendar Sync from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    # This will store data per config entry (e.g., coordinators)
    hass.data[DOMAIN][entry.entry_id] = {"coordinators": {}}

    # --- Universal Webhook Setup ---
    # We only register one webhook for the entire domain.
    if not hass.data[DOMAIN].get("webhook_registered"):
        _LOGGER.info(f"Registering universal webhook at /api/webhook/{UNIVERSAL_WEBHOOK_ID}")
        try:
            hass.components.webhook.async_register(
                DOMAIN, "ERP Calendar Sync", UNIVERSAL_WEBHOOK_ID, handle_webhook
            )
            hass.data[DOMAIN]["webhook_registered"] = True
        except ValueError:
            _LOGGER.warning("Universal webhook was already registered. This is normal on restart.")

    # Forward the setup to the calendar platform.
    # The calendar platform will handle fetching bookables and creating entities.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload the platform(s)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    # We don't unregister the universal webhook here, as other config entries
    # might still be using it. A more advanced implementation could track usage.
    # For now, it stays registered until HA shutdown.

    return unload_ok


async def handle_webhook(hass: HomeAssistant, webhook_id: str, request: web.Request):
    """Handle the universal incoming webhook."""
    try:
        data = await request.json()
        bookable_id = data.get("id")

        if not bookable_id:
            _LOGGER.warning("Webhook received without an 'id' field in JSON body")
            return web.Response(text="Missing 'id' in JSON body", status=400)

        _LOGGER.info(f"Webhook received for bookable ID: {bookable_id}")

        # Find the coordinator associated with this bookable ID
        # We have to check all configured entries for this domain
        found = False
        for entry_id, entry_data in hass.data[DOMAIN].items():
            if isinstance(entry_data, dict) and "coordinators" in entry_data:
                coordinator = entry_data["coordinators"].get(str(bookable_id))
                if coordinator:
                    _LOGGER.info(f"Found coordinator for {bookable_id}. Requesting refresh.")
                    await coordinator.async_request_refresh()
                    found = True
                    break # Found it, no need to search further
        
        if found:
            return web.Response(text=f"Refresh triggered for {bookable_id}.", status=200)
        else:
            _LOGGER.warning(f"Could not find a calendar for bookable ID: {bookable_id}")
            return web.Response(text="Bookable ID not found.", status=404)

    except Exception as e:
        _LOGGER.exception("Error processing webhook")
        return web.Response(text=f"Error: {e}", status=500)

