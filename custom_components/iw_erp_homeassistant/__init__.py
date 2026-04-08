"""The inwendo ERP / vynst integration."""
import logging

import aiohttp
from aiohttp import web
from icalendar import Calendar as iCalCalendar

from homeassistant.components import webhook
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import get_url

from .api import extract_erp_error_headers, log_api_error, read_body_snippet
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
            webhook.async_register(
                hass, DOMAIN, "ERP Calendar Sync", UNIVERSAL_WEBHOOK_ID, handle_webhook
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
    """Register our webhook URL with the ERP server. Returns True on success.

    Failure is non-fatal: the integration falls back to polling. Every failure
    mode still emits a single structured log line via log_api_error so the
    reason is visible without enabling debug logging.
    """
    host = entry.data[CONF_HOST]
    token = entry.data[CONF_TOKEN]
    url = f"{host}/api/homeassistant/webhook"
    try:
        ha_url = get_url(hass, prefer_external=True)
        webhook_url = f"{ha_url}/api/webhook/{UNIVERSAL_WEBHOOK_ID}"

        session = async_get_clientsession(hass)

        async with session.post(
            url,
            headers={"x-iw-jwt-token": token},
            json={"webhook_url": webhook_url},
            timeout=10,
        ) as resp:
            if resp.status == 200:
                try:
                    data = await resp.json(content_type=None)
                    _LOGGER.info("ERP webhook registered: %s", data.get("status"))
                except (ValueError, aiohttp.ContentTypeError):
                    _LOGGER.info("ERP webhook registered (no JSON body)")
                return True

            erp_code, erp_detail = extract_erp_error_headers(resp)
            body = await read_body_snippet(resp)
            synthetic = aiohttp.ClientResponseError(
                request_info=resp.request_info,
                history=resp.history,
                status=resp.status,
                message=resp.reason or "",
                headers=resp.headers,
            )
            log_api_error(
                _LOGGER,
                "Register ERP webhook",
                url,
                synthetic,
                status=resp.status,
                body_snippet=body,
                erp_code=erp_code,
                erp_detail=erp_detail,
                level=logging.WARNING,
            )
            return False
    except Exception as exc:  # noqa: BLE001 - classified inside log_api_error
        log_api_error(
            _LOGGER,
            "Register ERP webhook",
            url,
            exc,
            level=logging.WARNING,
        )
        return False


async def _unregister_erp_webhook(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Unregister our webhook URL from the ERP server.

    Runs during unload, so failures are logged at DEBUG level to avoid noisy
    shutdown errors but still contain full diagnostics if the user needs them.
    """
    host = entry.data[CONF_HOST]
    token = entry.data[CONF_TOKEN]
    url = f"{host}/api/homeassistant/webhook"
    try:
        ha_url = get_url(hass, prefer_external=True)
        webhook_url = f"{ha_url}/api/webhook/{UNIVERSAL_WEBHOOK_ID}"

        session = async_get_clientsession(hass)

        async with session.delete(
            url,
            headers={"x-iw-jwt-token": token},
            json={"webhook_url": webhook_url},
            timeout=10,
        ) as resp:
            if resp.status == 200:
                _LOGGER.info("ERP webhook unregistered")
                return

            erp_code, erp_detail = extract_erp_error_headers(resp)
            body = await read_body_snippet(resp)
            synthetic = aiohttp.ClientResponseError(
                request_info=resp.request_info,
                history=resp.history,
                status=resp.status,
                message=resp.reason or "",
                headers=resp.headers,
            )
            log_api_error(
                _LOGGER,
                "Unregister ERP webhook",
                url,
                synthetic,
                status=resp.status,
                body_snippet=body,
                erp_code=erp_code,
                erp_detail=erp_detail,
                level=logging.DEBUG,
            )
    except Exception as exc:  # noqa: BLE001 - classified inside log_api_error
        log_api_error(
            _LOGGER,
            "Unregister ERP webhook",
            url,
            exc,
            level=logging.DEBUG,
        )


async def _fetch_single_booking(hass: HomeAssistant, entry_data: dict, booking_id: str):
    """Fetch single booking iCal and bookable_id from the ERP API.

    Returns (bookable_id, ical_calendar) or (None, None) on failure.
    """
    host = entry_data.get("host")
    token = entry_data.get("token")
    if not host or not token:
        return None, None

    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"{host}/api/homeassistant/booking/{booking_id}",
            headers={"x-iw-jwt-token": token},
        ) as resp:
            if resp.status == 200:
                bookable_id = resp.headers.get("X-Bookable-Id")
                text = await resp.text()
                cal = iCalCalendar.from_ical(text)
                return bookable_id, cal
            elif resp.status == 404:
                # Booking was deleted - return bookable_id=None so we
                # can't do targeted update, will fall back to full refresh
                return None, None
    except Exception:
        _LOGGER.debug(f"Could not fetch booking {booking_id}")
    return None, None


def _patch_calendar(existing_cal: iCalCalendar, new_event_cal: iCalCalendar) -> iCalCalendar:
    """Merge a single-event iCal into an existing calendar.

    Replaces any existing VEVENT with the same UID, or adds the new event.
    """
    # Extract the new event's UID
    new_events = [c for c in new_event_cal.walk() if c.name == "VEVENT"]
    if not new_events:
        return existing_cal

    new_event = new_events[0]
    new_uid = str(new_event.get("uid", ""))

    # Build a new calendar with non-VEVENT components + filtered VEVENTs
    result = iCalCalendar()
    for key, value in existing_cal.items():
        result.add(key, value)

    # Copy existing events, skipping the one with matching UID
    for component in existing_cal.walk():
        if component.name == "VEVENT":
            existing_uid = str(component.get("uid", ""))
            if existing_uid != new_uid:
                result.add_component(component)

    # Add the new/updated event
    result.add_component(new_event)
    return result


def _remove_event_from_calendar(existing_cal: iCalCalendar, uid_to_remove: str) -> iCalCalendar:
    """Remove a VEVENT with a given UID from the calendar."""
    result = iCalCalendar()
    for key, value in existing_cal.items():
        result.add(key, value)

    for component in existing_cal.walk():
        if component.name == "VEVENT":
            if str(component.get("uid", "")) != uid_to_remove:
                result.add_component(component)

    return result


async def handle_webhook(hass: HomeAssistant, webhook_id: str, request: web.Request):
    """Handle incoming webhook from ERP server.

    Fetches the single booking iCal via /api/homeassistant/booking/{id}
    and patches it into the correct coordinator's calendar data,
    avoiding a full calendar reload.
    """
    try:
        data = await request.json()
        booking_id = data.get("iw_entity_id")
        action = data.get("iw_action", "unknown")
        _LOGGER.info(f"Webhook received: action={action}, booking_id={booking_id}")

        if not booking_id:
            return web.Response(text="No booking ID in payload", status=200)

        # Try to fetch the single booking and patch the calendar
        patched = False
        for entry_id, entry_data in hass.data[DOMAIN].items():
            if not isinstance(entry_data, dict) or "coordinators" not in entry_data:
                continue

            bookable_id, event_cal = await _fetch_single_booking(
                hass, entry_data, str(booking_id)
            )

            if bookable_id and event_cal:
                coordinator = entry_data["coordinators"].get(str(bookable_id))
                if coordinator and coordinator.data:
                    updated_cal = _patch_calendar(coordinator.data, event_cal)
                    coordinator.async_set_updated_data(updated_cal)
                    _LOGGER.info(f"Patched booking {booking_id} into calendar {bookable_id}")
                    patched = True
                    break

        # Fallback: if we couldn't patch (e.g. delete, or booking not found),
        # do a full refresh of all coordinators
        if not patched:
            for entry_id, entry_data in hass.data[DOMAIN].items():
                if isinstance(entry_data, dict) and "coordinators" in entry_data:
                    for coordinator in entry_data["coordinators"].values():
                        await coordinator.async_request_refresh()
            _LOGGER.info(f"Fallback: full refresh for action={action}, booking={booking_id}")

        return web.Response(text="OK", status=200)

    except Exception as e:
        _LOGGER.exception("Error processing webhook")
        return web.Response(text=f"Error: {e}", status=500)
