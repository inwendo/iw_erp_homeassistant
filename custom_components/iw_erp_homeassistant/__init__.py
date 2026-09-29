"""The inwendo ERP / vynst integration."""
from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import aiohttp
from aiohttp import web
from icalendar import Calendar as iCalCalendar

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import get_url

from .api import (
    client_timeout,
    extract_erp_error_headers,
    http_status_error,
    log_api_error,
    read_body_snippet,
)
from .const import (
    CONF_EXPOSE_HA_LOCKS,
    CONF_HOST,
    CONF_IMPORT_ERP_LOCKS,
    CONF_LOCK_SECRET,
    CONF_TOKEN,
    DOMAIN,
    LOCK_COMMAND_TYPE,
    SIGNATURE_HEADER,
    UNIVERSAL_WEBHOOK_ID,
)
from .lock_export import HaLockExporter, async_unregister_ha_locks, exposed_entity_ids

_LOGGER = logging.getLogger(__name__)


@dataclass
class IwErpData:
    """Runtime data of a config entry."""

    host: str
    token: str
    coordinators: dict[str, Any] = field(default_factory=dict)
    bookable_names: dict[str, str] = field(default_factory=dict)
    webhook_active: bool = False
    lock_coordinator: Any = None
    exporter: HaLockExporter | None = None


type IwErpConfigEntry = ConfigEntry[IwErpData]


def _platforms(entry: ConfigEntry) -> list[str]:
    platforms = ["calendar"]
    if entry.options.get(CONF_IMPORT_ERP_LOCKS):
        platforms.append("lock")
    return platforms


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Add the lock command secret to entries created before 1.2."""
    if entry.version == 1 and entry.minor_version < 2:
        data = {**entry.data}
        data.setdefault(CONF_LOCK_SECRET, secrets.token_hex(32))
        hass.config_entries.async_update_entry(entry, data=data, minor_version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: IwErpConfigEntry) -> bool:
    """Set up ERP Calendar Sync from a config entry."""
    entry.runtime_data = IwErpData(host=entry.data[CONF_HOST], token=entry.data[CONF_TOKEN])

    # --- Universal Webhook Setup ---
    # We only register one webhook for the entire domain.
    domain_data = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get("webhook_registered"):
        _LOGGER.info("Registering universal webhook at /api/webhook/%s", UNIVERSAL_WEBHOOK_ID)
        try:
            webhook.async_register(
                hass,
                DOMAIN,
                "ERP Calendar Sync",
                UNIVERSAL_WEBHOOK_ID,
                handle_webhook,
                allowed_methods=["POST"],
            )
        except ValueError:
            _LOGGER.debug("Universal webhook was already registered")
        domain_data["webhook_registered"] = True

    # Calendars: discover the bookables and load them (may raise auth failure / not ready).
    from .calendar import async_prepare_calendars

    await async_prepare_calendars(hass, entry)

    # Opt-in: ERP smart locks as lock entities.
    if entry.options.get(CONF_IMPORT_ERP_LOCKS):
        from .lock import ErpLockCoordinator

        coordinator = ErpLockCoordinator(hass, entry)
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data.lock_coordinator = coordinator

    # Forward the setup to the platforms.
    await hass.config_entries.async_forward_entry_setups(entry, _platforms(entry))

    # --- Register webhook with ERP server ---
    webhook_ok = await _register_erp_webhook(hass, entry)
    entry.runtime_data.webhook_active = webhook_ok

    # If webhook is active, slow down polling to 12 hours
    if webhook_ok:
        for coordinator in entry.runtime_data.coordinators.values():
            coordinator.update_interval = timedelta(hours=12)
        _LOGGER.info("Webhook active: polling interval set to 12 hours")

    # Opt-in: offer Home Assistant locks to the ERP.
    if exposed_entity_ids(hass, entry):
        exporter = HaLockExporter(hass, entry)
        entry.runtime_data.exporter = exporter
        await exporter.async_start()
        entry.async_on_unload(exporter.async_stop)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: IwErpConfigEntry) -> None:
    """Apply changed options: withdraw offered locks when switched off, then reload."""
    exporter = entry.runtime_data.exporter if entry.state is ConfigEntryState.LOADED else None
    if exporter is not None and not exposed_entity_ids(hass, entry):
        await exporter.async_unregister()
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: IwErpConfigEntry) -> bool:
    """Unload a config entry."""
    # Unregister webhook from ERP server
    await _unregister_erp_webhook(hass, entry)

    # Unload the platform(s)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, _platforms(entry))

    if unload_ok and not [
        other
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id and other.state is ConfigEntryState.LOADED
    ]:
        webhook.async_unregister(hass, UNIVERSAL_WEBHOOK_ID)
        hass.data.get(DOMAIN, {}).pop("webhook_registered", None)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Integration removed: withdraw the locks it offered to the ERP."""
    if entry.options.get(CONF_EXPOSE_HA_LOCKS) and entry.data.get(CONF_LOCK_SECRET):
        await async_unregister_ha_locks(
            hass, entry.data[CONF_HOST], entry.data[CONF_TOKEN], entry.data[CONF_LOCK_SECRET]
        )


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
            timeout=client_timeout(10),
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
            synthetic = http_status_error(resp)
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
            timeout=client_timeout(10),
        ) as resp:
            if resp.status == 200:
                _LOGGER.info("ERP webhook unregistered")
                return

            erp_code, erp_detail = extract_erp_error_headers(resp)
            body = await read_body_snippet(resp)
            synthetic = http_status_error(resp)
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


async def _fetch_single_booking(hass: HomeAssistant, entry_data: IwErpData, booking_id: str):
    """Fetch single booking iCal and bookable_id from the ERP API.

    Returns (bookable_id, ical_calendar) or (None, None) on failure.
    """
    host = entry_data.host
    token = entry_data.token
    if not host or not token:
        return None, None

    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"{host}/api/homeassistant/booking/{booking_id}",
            headers={"x-iw-jwt-token": token},
            timeout=client_timeout(15),
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


def _loaded_entries(hass: HomeAssistant) -> list[IwErpConfigEntry]:
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED and isinstance(getattr(entry, "runtime_data", None), IwErpData)
    ]


async def handle_webhook(hass: HomeAssistant, webhook_id: str, request: web.Request):
    """Handle incoming webhook from ERP server.

    Signed lock commands (``type: iw_lock_command``) go to the lock exporter.
    Everything else is a booking notification: fetch the single booking iCal
    via /api/homeassistant/booking/{id} and patch it into the correct
    coordinator's calendar data, avoiding a full calendar reload.
    """
    try:
        body = await request.read()
        try:
            data = json.loads(body) if body else {}
        except ValueError:
            return web.Response(text="Invalid JSON", status=400)
        if not isinstance(data, dict):
            return web.Response(text="Invalid payload", status=400)

        if data.get("type") == LOCK_COMMAND_TYPE:
            return await _handle_lock_command(hass, body, request.headers.get(SIGNATURE_HEADER))

        booking_id = data.get("iw_entity_id")
        action = data.get("iw_action", "unknown")
        _LOGGER.info("Webhook received: action=%s, booking_id=%s", action, booking_id)

        if not booking_id:
            return web.Response(text="No booking ID in payload", status=200)

        entries = _loaded_entries(hass)

        # Try to fetch the single booking and patch the calendar
        patched = False
        for entry in entries:
            entry_data = entry.runtime_data
            bookable_id, event_cal = await _fetch_single_booking(
                hass, entry_data, str(booking_id)
            )

            if bookable_id and event_cal:
                coordinator = entry_data.coordinators.get(str(bookable_id))
                if coordinator and coordinator.data:
                    updated_cal = _patch_calendar(coordinator.data, event_cal)
                    coordinator.async_set_updated_data(updated_cal)
                    _LOGGER.info("Patched booking %s into calendar %s", booking_id, bookable_id)
                    patched = True
                    break

        # Fallback: if we couldn't patch (e.g. delete, or booking not found),
        # do a full refresh of all coordinators
        if not patched:
            for entry in entries:
                for coordinator in entry.runtime_data.coordinators.values():
                    await coordinator.async_request_refresh()
            _LOGGER.info("Fallback: full refresh for action=%s, booking=%s", action, booking_id)

        return web.Response(text="OK", status=200)

    except Exception:
        _LOGGER.exception("Error processing webhook")
        return web.Response(text="Error", status=500)


async def _handle_lock_command(hass: HomeAssistant, body: bytes, signature: str | None) -> web.Response:
    """Pass a lock command to the exporter whose secret signed it."""
    for entry in _loaded_entries(hass):
        exporter = entry.runtime_data.exporter
        if exporter is not None and exporter.verify_signature(body, signature):
            status, payload = await exporter.async_handle_command(body, signature)
            return web.json_response(payload, status=status)
    _LOGGER.warning("Rejected ERP lock command: no matching signature or locks are not offered")
    return web.json_response({"success": False, "error": "invalid_signature"}, status=401)
