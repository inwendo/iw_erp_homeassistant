"""Calendar platform for ERP Calendar Sync."""
import logging
from datetime import timedelta, datetime

import aiohttp
from icalendar import Calendar as iCalCalendar

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .api import (
    AUTH_ERROR_KEYS,
    ERR_INVALID_RESPONSE,
    api_get_json,
    extract_erp_error_headers,
    log_api_error,
    read_body_snippet,
    sanitize_url,
)
from .const import DOMAIN, CONF_HOST, CONF_TOKEN

_LOGGER = logging.getLogger(__name__)

# Set a reasonable scan interval for polling, which the webhook can override.
SCAN_INTERVAL = timedelta(minutes=15)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the calendar platform for ERP Calendar Sync based on a config entry."""

    host = entry.data[CONF_HOST]
    token = entry.data[CONF_TOKEN]
    session = async_get_clientsession(hass)

    # --- Discover bookable calendars from the ERP ---
    url = f"{host}/api/homeassistant/bookables"
    _LOGGER.info("Fetching bookable resources from %s", sanitize_url(url))

    bookables, error = await api_get_json(
        session,
        url,
        token,
        _LOGGER,
        operation="Fetch bookable resources",
        timeout=15,
    )
    if error is not None:
        detail_parts = [error.key]
        if error.erp_code:
            detail_parts.append(f"erp_code={error.erp_code}")
        if error.erp_detail:
            detail_parts.append(f"erp_detail={error.erp_detail}")
        detail = ", ".join(detail_parts)

        # Auth failures must trigger a reauth flow rather than a silent retry
        # loop - ConfigEntryNotReady would otherwise spin forever on a
        # permanently invalid token.
        if error.key in AUTH_ERROR_KEYS:
            raise ConfigEntryAuthFailed(f"Cannot fetch bookables ({detail})")

        # Transient failures (timeout / 5xx / DNS blip) benefit from HA's
        # exponential-backoff retry via ConfigEntryNotReady.
        raise ConfigEntryNotReady(f"Cannot fetch bookables ({detail})")
    if not isinstance(bookables, list):
        _LOGGER.error(
            "Fetch bookable resources failed: key=%s url=%s "
            "reason=unexpected_response_shape type=%s",
            ERR_INVALID_RESPONSE,
            sanitize_url(url),
            type(bookables).__name__,
        )
        return

    entities = []
    coordinators_map = hass.data[DOMAIN][entry.entry_id]["coordinators"]

    for bookable in bookables:
        bookable_id = str(bookable.get("id"))
        bookable_name = bookable.get("name")

        if not all([bookable_id, bookable_name]):
            _LOGGER.warning(f"Skipping a bookable due to missing 'id' or 'name': {bookable}")
            continue

        calendar_url = f"{host}/api/homeassistant/calendar/{bookable_id}?deleted=0&onlyFutureEndTime=1&page=0"

        def _make_update_method(url: str, name: str):
            """Create an update method with captured variables."""
            operation = f"Fetch calendar for {name}"
            safe_url = sanitize_url(url)

            async def async_update_data():
                """Fetch data for a single calendar."""
                try:
                    _LOGGER.debug(
                        "Fetching calendar data for %s from %s", name, safe_url
                    )
                    async with session.get(
                        url,
                        headers={"x-iw-jwt-token": token},
                        timeout=15,
                    ) as resp:
                        status = resp.status
                        erp_code, erp_detail = extract_erp_error_headers(resp)
                        if status >= 400:
                            body = await read_body_snippet(resp)
                            synthetic = aiohttp.ClientResponseError(
                                request_info=resp.request_info,
                                history=resp.history,
                                status=status,
                                message=resp.reason or "",
                                headers=resp.headers,
                            )
                            error = log_api_error(
                                _LOGGER,
                                operation,
                                url,
                                synthetic,
                                status=status,
                                body_snippet=body,
                                erp_code=erp_code,
                                erp_detail=erp_detail,
                            )
                            msg = f"{operation}: HTTP {status} ({error.key})"
                            if error.erp_code:
                                msg += f" [{error.erp_code}]"
                            raise UpdateFailed(msg)
                        text = await resp.text()
                        try:
                            return iCalCalendar.from_ical(text)
                        except Exception as parse_err:  # noqa: BLE001
                            log_api_error(
                                _LOGGER,
                                operation,
                                url,
                                parse_err,
                                status=status,
                                body_snippet=text[:500],
                                erp_code=erp_code,
                                erp_detail=erp_detail,
                            )
                            raise UpdateFailed(
                                f"{operation}: invalid iCal "
                                f"({type(parse_err).__name__})"
                            )
                except UpdateFailed:
                    raise
                except Exception as err:  # noqa: BLE001 - classified below
                    error = log_api_error(_LOGGER, operation, url, err)
                    raise UpdateFailed(
                        f"{operation}: {type(err).__name__} ({error.key})"
                    )
            return async_update_data

        coordinator = DataUpdateCoordinator(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{bookable_name}",
            update_method=_make_update_method(calendar_url, bookable_name),
            update_interval=SCAN_INTERVAL,
        )

        # Store coordinator for the webhook to access via bookable_id
        coordinators_map[bookable_id] = coordinator

        # Fetch initial data
        await coordinator.async_config_entry_first_refresh()

        entities.append(ERPCalendarEntity(coordinator, entry.entry_id, bookable_id, bookable_name))

    async_add_entities(entities, True)


class ERPCalendarEntity(CalendarEntity):
    """A calendar entity for an ERP room booking."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        config_id: str,
        bookable_id: str,
        bookable_name: str
    ):
        """Initialize the ERPCalendarEntity."""
        self.coordinator = coordinator
        self._config_id = config_id
        self._bookable_id = bookable_id
        self._name = bookable_name
        self._event = None

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return f"{self._config_id}-{self._bookable_id}"

    @property
    def name(self) -> str:
        """Return the name of the entity."""
        return self._name

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event."""
        return self._event

    @property
    def state(self) -> str | None:
        """Return the state of the calendar."""
        if self.event and self.event.start <= dt_util.now() < self.event.end:
            return "on"
        return "off"

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Get all events in a specific time frame."""
        events = []
        calendar: iCalCalendar = self.coordinator.data
        if not calendar:
            return events

        for component in calendar.walk():
            if component.name == "VEVENT":
                try:
                    event_start = component.get("dtstart").dt
                    event_end = component.get("dtend").dt

                    if not isinstance(event_start, datetime):
                        event_start = datetime.combine(event_start, datetime.min.time(), tzinfo=dt_util.get_default_time_zone())
                    if not isinstance(event_end, datetime):
                        event_end = datetime.combine(event_end, datetime.min.time(), tzinfo=dt_util.get_default_time_zone())

                    if event_start < end_date and event_end > start_date:
                        events.append(
                            CalendarEvent(
                                start=event_start,
                                end=event_end,
                                summary=str(component.get("summary", "")),
                                description=str(component.get("description", "")),
                                location=str(component.get("location", "")),
                            )
                        )
                except Exception:
                    _LOGGER.warning(f"Could not parse event from calendar {self.name}")

        return sorted(events, key=lambda e: e.start)

    def _update_internal_state(self) -> None:
        """Update the internal state to find the current or next event."""
        calendar: iCalCalendar = self.coordinator.data
        now = dt_util.now()

        if not calendar:
            self._event = None
            return

        # We need to parse all events to find the current or next one
        all_events = []
        for component in calendar.walk():
            if component.name == "VEVENT":
                try:
                    start = component.get('dtstart').dt
                    end = component.get('dtend').dt

                    if not isinstance(start, datetime):
                       start = datetime.combine(start, datetime.min.time(), tzinfo=now.tzinfo)
                    if not isinstance(end, datetime):
                        end = datetime.combine(end, datetime.min.time(), tzinfo=now.tzinfo)

                    # Only consider future or current events
                    if end > now:
                        all_events.append(CalendarEvent(
                            start=start,
                            end=end,
                            summary=str(component.get("summary", "")),
                        ))
                except Exception:
                    continue

        # Sort events to easily find the next one
        all_events.sort(key=lambda e: e.start)

        # Find the first active or future event
        for event in all_events:
            if event.start <= now < event.end: # Active event
                self._event = event
                return
            if event.start > now: # First upcoming event
                self._event = event
                return

        # If no active or future events are found
        self._event = None

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self._handle_coordinator_update() # Initial update

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_internal_state()
        self.async_write_ha_state()
