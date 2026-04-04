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
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinators": {},
        "webhook_active": False,
        "host": entry.data[CONF_HOST],
        "token": entry.data[CONF_TOKEN],
    }

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
    webhook_ok = await _register_erp_webhook(hass, entry)
    hass.data[DOMAIN][entry.entry_id]["webhook_active"] = webhook_ok

    # If webhook is active, slow down polling to 12 hours
    if webhook_ok:
        from datetime import timedelta
        for coordinator in hass.data[DOMAIN][entry.entry_id]["coordinators"].values():
            coordinator.update_interval = timedelta(hours=12)
        _LOGGER.info("Webhook active: polling interval set to 12 hours")

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


async def _register_erp_webhook(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Register our webhook URL with the ERP server. Returns True on success."""
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
                return True
            else:
                _LOGGER.warning(f"Failed to register ERP webhook: HTTP {resp.status}")
                return False
    except Exception:
        _LOGGER.warning("Could not register webhook with ERP server. Using polling only.")
        return False


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


async def _resolve_bookable_id(hass: HomeAssistant, entry_data: dict, booking_id: str) -> str | None:
    """Look up which bookable a booking belongs to via the ERP API."""
    host = entry_data.get("host")
    token = entry_data.get("token")
    if not host or not token:
        return None

    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"{host}/api/homeassistant/booking/{booking_id}",
            headers={"x-iw-jwt-token": token},
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return str(data.get("bookable_id")) if data.get("bookable_id") else None
    except Exception:
        _LOGGER.debug(f"Could not resolve bookable for booking {booking_id}")
    return None


async def handle_webhook(hass: HomeAssistant, webhook_id: str, request: web.Request):
    """Handle incoming webhook from ERP server.

    The ERP webhook sends iw_entity_id (booking ID). We look up the
    bookable_id via a lightweight API call, then refresh only that
    coordinator. Falls back to refreshing all if lookup fails.
    """
    try:
        data = await request.json()
        booking_id = data.get("iw_entity_id")
        action = data.get("iw_action", "unknown")
        _LOGGER.info(f"Webhook received: action={action}, booking_id={booking_id}")

        # Try to resolve the bookable_id for targeted refresh
        refreshed = False
        if booking_id:
            for entry_id, entry_data in hass.data[DOMAIN].items():
                if not isinstance(entry_data, dict) or "coordinators" not in entry_data:
                    continue

                bookable_id = await _resolve_bookable_id(hass, entry_data, str(booking_id))
                if bookable_id:
                    coordinator = entry_data["coordinators"].get(bookable_id)
                    if coordinator:
                        await coordinator.async_request_refresh()
                        _LOGGER.info(f"Refreshed calendar for bookable {bookable_id}")
                        refreshed = True
                        break

        # Fallback: refresh all coordinators if targeted refresh failed
        if not refreshed:
            count = 0
            for entry_id, entry_data in hass.data[DOMAIN].items():
                if isinstance(entry_data, dict) and "coordinators" in entry_data:
                    for coordinator in entry_data["coordinators"].values():
                        await coordinator.async_request_refresh()
                        count += 1
            _LOGGER.info(f"Fallback: refreshed all {count} calendars")

        return web.Response(text="OK", status=200)

    except Exception as e:
        _LOGGER.exception("Error processing webhook")
        return web.Response(text=f"Error: {e}", status=500)
