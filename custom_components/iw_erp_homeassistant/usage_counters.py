"""ERP usage counters (opt-in: "Usage counters").

Two directions:

* every ERP usage counter (``GET /api/homeassistant/usage_counters``) becomes a
  read-only sensor, refreshed every 15 minutes;
* Home Assistant meters the user mapped to a counter in the options are
  reported with ``POST /api/homeassistant/usage_counters/{id}`` when they
  change - only numeric readings, at most once per :data:`REPORT_INTERVAL`
  per counter (a reading that arrives earlier is sent when the interval is
  over, the latest one wins).

The ERP rejects reports for counters fed by real usage data
(``use_real_usage_data``); those are not offered in the options and a
rejection stops the reports for that counter until the next reload.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import api_get_json, api_request_json, is_auth_rejected
from .issues import async_api_key_rejected
from .const import CONF_HOST, CONF_TOKEN, CONF_USAGE_COUNTER_MAP, DOMAIN, FEATURE_USAGE_COUNTERS

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=15)
DISABLED_INTERVAL = timedelta(hours=1)
# At most one report per counter in this time.
REPORT_INTERVAL = timedelta(minutes=15)


async def async_fetch_usage_counters(
    hass: HomeAssistant, host: str, token: str
) -> tuple[list[dict[str, Any]] | None, Any]:
    """The ERP's usage counters, or (None, ApiError) when the ERP cannot tell."""
    data, error = await api_get_json(
        async_get_clientsession(hass),
        f"{host}/api/homeassistant/usage_counters",
        token,
        _LOGGER,
        "Fetch ERP usage counters",
        timeout=15,
    )
    if error is not None:
        return None, error
    if not isinstance(data, list):
        return None, None
    return [c for c in data if isinstance(c, dict) and c.get("id") is not None], None


def configured_counter_map(options: dict[str, Any]) -> dict[str, str]:
    """ERP usage counter id -> Home Assistant sensor entity id, from the options."""
    mapping = options.get(CONF_USAGE_COUNTER_MAP) or {}
    if not isinstance(mapping, dict):
        return {}
    return {str(k): str(v) for k, v in mapping.items() if k and v}


def numeric_state(value: str | None) -> float | None:
    """A finite number from a state, else None (unknown, unavailable, text)."""
    if value in (None, "", STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class UsageCounterCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Counter id -> usage counter as the ERP sends it."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_usage_counters",
            update_interval=SCAN_INTERVAL,
        )
        self.host: str = entry.data[CONF_HOST]
        self.token: str = entry.data[CONF_TOKEN]
        self.features = entry.runtime_data.features

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        counters, error = await async_fetch_usage_counters(self.hass, self.host, self.token)
        if error is not None:
            if self.features.handle_error(FEATURE_USAGE_COUNTERS, error):
                self.update_interval = DISABLED_INTERVAL
                return {}
            if is_auth_rejected(error):
                async_api_key_rejected(self.hass, self.config_entry)
                raise ConfigEntryAuthFailed(f"Cannot fetch usage counters ({error.key})")
            raise UpdateFailed(f"Cannot fetch usage counters ({error.key})")
        if counters is None:
            raise UpdateFailed("Unexpected usage counter list")
        self.features.ok(FEATURE_USAGE_COUNTERS)
        self.update_interval = SCAN_INTERVAL
        return {str(c["id"]): c for c in counters}


class UsageCounterReporter:
    """Reports mapped Home Assistant meters to their ERP usage counters."""

    def __init__(self, hass: HomeAssistant, entry, coordinator: UsageCounterCoordinator | None = None) -> None:
        self.hass = hass
        self.entry = entry
        self.host: str = entry.data[CONF_HOST]
        self.token: str = entry.data[CONF_TOKEN]
        self.features = entry.runtime_data.features
        self.coordinator = coordinator
        self.mapping = configured_counter_map(entry.options)
        self.last_sent: dict[str, datetime] = {}
        self.last_value: dict[str, float] = {}
        self.stopped_counters: set[str] = set()
        self._pending: dict[str, CALLBACK_TYPE] = {}
        self._unsub: CALLBACK_TYPE | None = None

    async def async_start(self) -> None:
        """Follow the mapped sensors and report their current readings."""
        if not self.mapping:
            return
        self._unsub = async_track_state_change_event(
            self.hass, list(set(self.mapping.values())), self._state_changed
        )
        for counter_id in self.mapping:
            await self.async_report(counter_id)

    @callback
    def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        for unsub in self._pending.values():
            unsub()
        self._pending.clear()

    @callback
    def _state_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        for counter_id, mapped in self.mapping.items():
            if mapped == entity_id:
                self.entry.async_create_background_task(
                    self.hass, self.async_report(counter_id), f"iw_erp usage counter {counter_id}"
                )

    async def async_report(self, counter_id: str) -> None:
        """Report the current reading of the counter's sensor, throttled."""
        if counter_id in self.stopped_counters or counter_id in self._pending:
            return
        if self.features.available.get(FEATURE_USAGE_COUNTERS) is False:
            return
        state = self.hass.states.get(self.mapping[counter_id])
        value = numeric_state(state.state if state else None)
        if value is None or self.last_value.get(counter_id) == value:
            return

        now = dt_util.utcnow()
        last = self.last_sent.get(counter_id)
        if last is not None and now - last < REPORT_INTERVAL:
            # Too early: send the then latest reading when the interval is over.
            wait = (last + REPORT_INTERVAL - now).total_seconds()

            @callback
            def _later(_now: datetime) -> None:
                self._pending.pop(counter_id, None)
                self.entry.async_create_background_task(
                    self.hass, self.async_report(counter_id), f"iw_erp usage counter {counter_id}"
                )

            self._pending[counter_id] = async_call_later(self.hass, wait, _later)
            return

        self.last_sent[counter_id] = now
        data, error = await api_request_json(
            async_get_clientsession(self.hass),
            "POST",
            f"{self.host}/api/homeassistant/usage_counters/{counter_id}",
            self.token,
            _LOGGER,
            f"Report usage counter {counter_id}",
            json_body={"value": value},
            timeout=15,
            level=logging.WARNING,
        )
        if error is not None:
            if self.features.handle_error(FEATURE_USAGE_COUNTERS, error):
                return
            if error.status == 400 and error.erp_code in (None, "400"):
                # e.g. a counter fed by real usage data: do not try again and again.
                self.stopped_counters.add(counter_id)
                _LOGGER.warning(
                    "The ERP refuses readings for usage counter %s (%s); stopped reporting it",
                    counter_id,
                    error.erp_detail or error.key,
                )
            return
        self.last_value[counter_id] = value
        if self.coordinator is not None and isinstance(data, dict) and self.coordinator.data is not None:
            counter = self.coordinator.data.get(counter_id)
            if counter is not None:
                updated = {**counter, "value": data.get("value", value)}
                if data.get("updated"):
                    updated["updated"] = data["updated"]
                self.coordinator.async_set_updated_data({**self.coordinator.data, counter_id: updated})
