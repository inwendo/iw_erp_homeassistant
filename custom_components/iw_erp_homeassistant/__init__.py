"""The inwendo ERP / vynst integration."""
from __future__ import annotations

import hmac
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
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.typing import ConfigType

from .api import (
    client_timeout,
    extract_erp_error_headers,
    http_status_error,
    log_api_error,
    read_body_snippet,
)
from .const import (
    CONF_ACCESS_EVENTS,
    CONF_DISPLAY_IMAGES,
    CONF_EXPOSE_HA_LOCKS,
    CONF_HOST,
    CONF_IMPORT_ERP_LOCKS,
    CONF_LOCK_SECRET,
    CONF_OCCUPANCY,
    CONF_TOKEN,
    CONF_USAGE_COUNTERS,
    DOMAIN,
    FEATURE_ACCESS_EVENTS,
    FEATURE_SETTINGS,
    LOCK_COMMAND_TYPE,
    SIGNATURE_HEADER,
    UNIVERSAL_WEBHOOK_ID,
    WEBHOOK_TOKEN_HEADER,
)
from .events import KIND_ACCESS, KIND_BOOKING, KIND_CHECK_IN, async_fire, classify, webhook_token
from .issues import (
    ISSUE_API_KEY_REJECTED,
    ISSUE_NO_EXTERNAL_URL,
    FeatureTracker,
    async_api_key_rejected,
    async_clear,
    async_clear_all,
    async_raise,
)
from .lock_export import HaLockExporter, async_unregister_ha_locks, exposed_entity_ids

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class IwErpData:
    """Runtime data of a config entry."""

    host: str
    token: str
    features: FeatureTracker
    coordinators: dict[str, Any] = field(default_factory=dict)
    bookable_names: dict[str, str] = field(default_factory=dict)
    webhook_active: bool = False
    # What the ERP confirmed besides bookings: "access", "check_in"
    webhook_events: list[str] = field(default_factory=list)
    webhook_token: str = ""
    lock_coordinator: Any = None
    exporter: HaLockExporter | None = None
    display_sender: Any = None
    display_names: dict[str, str] = field(default_factory=dict)
    occupancy: Any = None
    usage_counters: Any = None
    usage_reporter: Any = None
    platforms: list[str] = field(default_factory=list)


type IwErpConfigEntry = ConfigEntry[IwErpData]


def _platforms(entry: ConfigEntry) -> list[str]:
    """The platforms the options need (sensor / binary_sensor / image only when used)."""
    data = entry.runtime_data
    platforms = ["calendar"]
    if entry.options.get(CONF_IMPORT_ERP_LOCKS):
        platforms.append("lock")
    if data.occupancy is not None:
        platforms.append("binary_sensor")
    if data.occupancy is not None or data.usage_counters is not None or entry.options.get(CONF_ACCESS_EVENTS):
        platforms.append("sensor")
    if data.display_sender is not None:
        platforms.append("image")
    return platforms


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services; they work for every loaded config entry."""
    from .services import async_setup_services

    async_setup_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Add the lock command secret to entries created before 1.2."""
    if entry.version == 1 and entry.minor_version < 2:
        data = {**entry.data}
        data.setdefault(CONF_LOCK_SECRET, secrets.token_hex(32))
        hass.config_entries.async_update_entry(entry, data=data, minor_version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: IwErpConfigEntry) -> bool:
    """Set up ERP Calendar Sync from a config entry."""
    entry.runtime_data = IwErpData(
        host=entry.data[CONF_HOST],
        token=entry.data[CONF_TOKEN],
        features=FeatureTracker(hass, entry.entry_id, entry.title),
        webhook_token=webhook_token(entry),
    )

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

    try:
        await async_prepare_calendars(hass, entry)
    except ConfigEntryAuthFailed:
        async_api_key_rejected(hass, entry)
        raise
    async_clear(hass, ISSUE_API_KEY_REJECTED, entry.entry_id)

    # Opt-in: ERP smart locks as lock entities.
    if entry.options.get(CONF_IMPORT_ERP_LOCKS):
        from .lock import ErpLockCoordinator

        coordinator = ErpLockCoordinator(hass, entry)
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data.lock_coordinator = coordinator

    # Opt-in: occupancy sensors per bookable. Never blocks the setup: an ERP
    # without the function just gives no sensors.
    if entry.options.get(CONF_OCCUPANCY):
        from .occupancy import OccupancyCoordinator

        occupancy = OccupancyCoordinator(hass, entry)
        await occupancy.async_refresh()
        entry.runtime_data.occupancy = occupancy

    # Opt-in: ERP usage counters as sensors, Home Assistant meters reported to them.
    if entry.options.get(CONF_USAGE_COUNTERS):
        from .usage_counters import UsageCounterCoordinator, UsageCounterReporter

        counters = UsageCounterCoordinator(hass, entry)
        await counters.async_refresh()
        entry.runtime_data.usage_counters = counters
        reporter = UsageCounterReporter(hass, entry, counters)
        entry.runtime_data.usage_reporter = reporter
        entry.async_on_unload(reporter.async_stop)

    # Room displays: uploaded to OpenDisplay panels (assigned in the options)
    # and / or only shown as image entities (option "Room display images").
    await _prepare_displays(hass, entry)

    # Forward the setup to the platforms.
    entry.runtime_data.platforms = _platforms(entry)
    await hass.config_entries.async_forward_entry_setups(entry, entry.runtime_data.platforms)

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

    # Room displays and meter reports start once Home Assistant runs, so the
    # OpenDisplay integration and the meters' integrations are set up by then.
    sender = entry.runtime_data.display_sender
    reporter = entry.runtime_data.usage_reporter

    @callback
    def _start_background(_event: Event | None = None) -> None:
        if sender is not None:
            entry.async_create_background_task(hass, sender.async_start(), "iw_erp displays")
        if reporter is not None:
            entry.async_create_background_task(hass, reporter.async_start(), "iw_erp usage counters")

    if sender is not None or reporter is not None:
        if hass.is_running:
            _start_background()
        else:
            entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _start_background))

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _prepare_displays(hass: HomeAssistant, entry: IwErpConfigEntry) -> None:
    """Create the room display sender when displays are assigned or images wanted."""
    from .display import ErpDisplaySender, async_fetch_erp_displays, configured_displays

    assigned = configured_displays(entry.options)
    if not assigned and not entry.options.get(CONF_DISPLAY_IMAGES):
        return
    displays = await async_fetch_erp_displays(hass, entry.data[CONF_HOST], entry.data[CONF_TOKEN]) or []
    names = {str(d["id"]): str(d.get("name") or f"Room display {d['id']}") for d in displays}
    image_only = [d for d in names if d not in assigned] if entry.options.get(CONF_DISPLAY_IMAGES) else []
    if not assigned and not image_only:
        return
    sender = ErpDisplaySender(hass, entry, image_only=image_only)
    entry.runtime_data.display_sender = sender
    entry.runtime_data.display_names = names
    entry.async_on_unload(sender.async_stop)


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

    # Unload the platform(s) that were set up
    unload_ok = await hass.config_entries.async_unload_platforms(entry, entry.runtime_data.platforms)

    if unload_ok and not [
        other
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id and other.state is ConfigEntryState.LOADED
    ]:
        webhook.async_unregister(hass, UNIVERSAL_WEBHOOK_ID)
        hass.data.get(DOMAIN, {}).pop("webhook_registered", None)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Integration removed: withdraw the locks it offered to the ERP, drop its repair issues."""
    async_clear_all(hass, entry.entry_id)
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
    ok = await _post_erp_webhook(hass, entry)
    if ok:
        async_clear(hass, ISSUE_NO_EXTERNAL_URL, entry.entry_id)
    elif not _has_external_https_url(hass):
        # The ERP only pushes to a public https URL: tell the user why it polls.
        async_raise(hass, ISSUE_NO_EXTERNAL_URL, entry.entry_id, entry.title)
    else:
        async_clear(hass, ISSUE_NO_EXTERNAL_URL, entry.entry_id)
    return ok


def _has_external_https_url(hass: HomeAssistant) -> bool:
    try:
        get_url(hass, allow_internal=False, allow_ip=False, require_ssl=True, prefer_cloud=True)
    except NoURLAvailableError:
        return False
    return True


def _handle_webhook_answer(entry: IwErpConfigEntry, data: Any) -> None:
    """Remember which events the ERP delivers besides bookings."""
    events = data.get("events") if isinstance(data, dict) else None
    entry.runtime_data.webhook_events = [e for e in events if isinstance(e, str)] if isinstance(events, list) else []
    if not entry.options.get(CONF_ACCESS_EVENTS):
        return
    features = entry.runtime_data.features
    if {"access", "check_in"} & set(entry.runtime_data.webhook_events):
        features.ok(FEATURE_ACCESS_EVENTS)
    elif FEATURE_ACCESS_EVENTS not in features.available:
        features.available[FEATURE_ACCESS_EVENTS] = False
        _LOGGER.info(
            "The ERP does not send access and check-in events (setting %s is off, or the ERP "
            "version does not offer them yet); no iw_erp_access / iw_erp_check_in events",
            FEATURE_SETTINGS[FEATURE_ACCESS_EVENTS],
        )


async def _post_erp_webhook(hass: HomeAssistant, entry: IwErpConfigEntry) -> bool:
    host = entry.data[CONF_HOST]
    token = entry.data[CONF_TOKEN]
    url = f"{host}/api/homeassistant/webhook"
    try:
        ha_url = get_url(hass, prefer_external=True)
        webhook_url = f"{ha_url}/api/webhook/{UNIVERSAL_WEBHOOK_ID}"
        body: dict[str, Any] = {
            "webhook_url": webhook_url,
            # Sent back by the ERP as X-IW-Webhook-Token: marks events as genuine.
            "webhook_token": entry.runtime_data.webhook_token,
        }
        if entry.options.get(CONF_ACCESS_EVENTS):
            body["events"] = ["access", "check_in"]

        session = async_get_clientsession(hass)

        async with session.post(
            url,
            headers={"x-iw-jwt-token": token},
            json=body,
            timeout=client_timeout(10),
        ) as resp:
            if resp.status == 200:
                try:
                    data = await resp.json(content_type=None)
                    _LOGGER.info("ERP webhook registered: %s", data.get("status"))
                except (ValueError, aiohttp.ContentTypeError, AttributeError):
                    _LOGGER.info("ERP webhook registered (no JSON body)")
                    data = None
                _handle_webhook_answer(entry, data)
                return True

            erp_code, erp_detail = extract_erp_error_headers(resp)
            snippet = await read_body_snippet(resp)
            synthetic = http_status_error(resp)
            log_api_error(
                _LOGGER,
                "Register ERP webhook",
                url,
                synthetic,
                status=resp.status,
                body_snippet=snippet,
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
    Smart lock log and attendance list entries (option "Access and check-in
    events") become ``iw_erp_access`` / ``iw_erp_check_in`` events.
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

        kind = classify(data)
        if kind in (KIND_ACCESS, KIND_CHECK_IN):
            return _handle_event_webhook(hass, kind, data, request.headers.get(WEBHOOK_TOKEN_HEADER))
        if kind != KIND_BOOKING:
            _LOGGER.debug("Ignored webhook for %s", data.get("iw_entity_class") or data.get("iw_entity_short_class"))
            return web.Response(text="Ignored", status=200)

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

        # A booking changed: the occupancy may have changed too.
        for entry in entries:
            if entry.runtime_data.occupancy is not None:
                await entry.runtime_data.occupancy.async_request_refresh()

        return web.Response(text="OK", status=200)

    except Exception:
        _LOGGER.exception("Error processing webhook")
        return web.Response(text="Error", status=500)


def _handle_event_webhook(hass: HomeAssistant, kind: str, data: dict, token: str | None) -> web.Response:
    """Fire the event of an access / check-in webhook for the entries that want it.

    When the ERP sends this installation's webhook token, the event is marked
    ``verified``; a wrong token is rejected. ERP versions that do not send the
    token yet still get through, unverified (the webhook URL is not secret).
    """
    entries = [e for e in _loaded_entries(hass) if e.options.get(CONF_ACCESS_EVENTS)]
    verified = False
    if token:
        matching = [e for e in entries if hmac.compare_digest(e.runtime_data.webhook_token, token.strip())]
        if not matching and entries:
            _LOGGER.warning("Rejected ERP %s webhook: wrong webhook token", kind)
            return web.Response(text="Invalid token", status=401)
        entries, verified = matching, bool(matching)
    async_fire(hass, kind, data, entries, verified)
    return web.Response(text="OK", status=200)


async def _handle_lock_command(hass: HomeAssistant, body: bytes, signature: str | None) -> web.Response:
    """Pass a lock command to the exporter whose secret signed it."""
    for entry in _loaded_entries(hass):
        exporter = entry.runtime_data.exporter
        if exporter is not None and exporter.verify_signature(body, signature):
            status, payload = await exporter.async_handle_command(body, signature)
            return web.json_response(payload, status=status)
    _LOGGER.warning("Rejected ERP lock command: no matching signature or locks are not offered")
    return web.json_response({"success": False, "error": "invalid_signature"}, status=401)
