"""The inwendo ERP / vynst integration."""
import logging
from aiohttp import web

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import get_url

from .const import DOMAIN, CONF_HOST, CONF_TOKEN

_LOGGER = logging.getLogger(__name__)

# Define the platforms we want to set up
PLATFORMS = ["calendar"]

# This is our universal webhook ID. The URL will be /api/webhook/iw_erp_homeassistant
UNIVERSAL_WEBHOOK_ID = DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ERP Calendar Sync from a config entry."""
    hass.data.setdefault(DOMAIN, {})
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
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # --- Register webhook with ERP server ---
    await _register_erp_webhook(hass, entry)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Unregister webhook from ERP server
    await _unregister_erp_webhook(hass, entry)

    # Unload the platform(s)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def _register_erp_webhook(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register our webhook URL with the ERP server so it can push booking updates."""
    try:
        ha_url = get_url(hass, prefer_external=True)
        webhook_url = f"{ha_url}/api/webhook/{UNIVERSAL_WEBHOOK_ID}"

        host = entry.data[CONF_HOST]
        token = entry.data[CONF_TOKEN]
        session = async_get_clientsession(hass)

        async with session.post(
            f"{host}/api/homeassistant/webhook",
            headers={"x-iw-jwt-token": token},
            json={"webhook_url": webhook_url},
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                _LOGGER.info(f"ERP webhook registered: {data.get('status')}")
            else:
                _LOGGER.warning(f"Failed to register ERP webhook: HTTP {resp.status}")
    except Exception:
        _LOGGER.warning("Could not register webhook with ERP server. Push updates will not work.")


async def _unregister_erp_webhook(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Unregister our webhook URL from the ERP server."""
    try:
        ha_url = get_url(hass, prefer_external=True)
        webhook_url = f"{ha_url}/api/webhook/{UNIVERSAL_WEBHOOK_ID}"

        host = entry.data[CONF_HOST]
        token = entry.data[CONF_TOKEN]
        session = async_get_clientsession(hass)

        async with session.delete(
            f"{host}/api/homeassistant/webhook",
            headers={"x-iw-jwt-token": token},
            json={"webhook_url": webhook_url},
        ) as resp:
            if resp.status == 200:
                _LOGGER.info("ERP webhook unregistered")
            else:
                _LOGGER.warning(f"Failed to unregister ERP webhook: HTTP {resp.status}")
    except Exception:
        _LOGGER.debug("Could not unregister webhook from ERP server.")


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
        found = False
        for entry_id, entry_data in hass.data[DOMAIN].items():
            if isinstance(entry_data, dict) and "coordinators" in entry_data:
                coordinator = entry_data["coordinators"].get(str(bookable_id))
                if coordinator:
                    _LOGGER.info(f"Found coordinator for {bookable_id}. Requesting refresh.")
                    await coordinator.async_request_refresh()
                    found = True
                    break

        if found:
            return web.Response(text=f"Refresh triggered for {bookable_id}.", status=200)
        else:
            _LOGGER.warning(f"Could not find a calendar for bookable ID: {bookable_id}")
            return web.Response(text="Bookable ID not found.", status=404)

    except Exception as e:
        _LOGGER.exception("Error processing webhook")
        return web.Response(text=f"Error: {e}", status=500)
