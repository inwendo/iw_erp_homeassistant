"""Diagnostics download (Settings > Devices & Services > inwendo ERP > ... > Download diagnostics).

API key, lock secret and webhook token are redacted; the host keeps only
scheme, host name and port (credentials typed into the URL are stripped).
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .api import sanitize_url
from .const import CONF_HOST, CONF_LOCK_SECRET, CONF_TOKEN, DOMAIN

# Booking titles and person names can be personal data.
TO_REDACT = {
    CONF_TOKEN, CONF_LOCK_SECRET, "webhook_token", "secret", "token", "password",
    "title", "person", "person_name",
}


def _coordinator_info(coordinator: Any) -> dict[str, Any] | None:
    if coordinator is None:
        return None
    return {
        "last_update_success": coordinator.last_update_success,
        "update_interval": str(coordinator.update_interval),
        "data": coordinator.data,
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = {**entry.data, CONF_HOST: sanitize_url(entry.data.get(CONF_HOST, ""))}
    result: dict[str, Any] = {
        "entry": {
            "title": sanitize_url(entry.title),
            "version": f"{entry.version}.{entry.minor_version}",
            "data": async_redact_data(data, TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "issues": [
            issue_id
            for (domain, issue_id) in ir.async_get(hass).issues
            if domain == DOMAIN and entry.entry_id in issue_id
        ],
    }
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return result

    result["runtime"] = {
        "bookables": runtime.bookable_names,
        "calendars": {
            bookable_id: {
                "last_update_success": coordinator.last_update_success,
                "update_interval": str(coordinator.update_interval),
                "events": len(list(coordinator.data.walk("VEVENT"))) if coordinator.data else 0,
            }
            for bookable_id, coordinator in runtime.coordinators.items()
        },
        "webhook_active": runtime.webhook_active,
        "webhook_events": runtime.webhook_events,
        "erp_features": runtime.features.available,
        "platforms": runtime.platforms,
        "smart_locks": _coordinator_info(runtime.lock_coordinator),
        "offered_ha_locks": runtime.exporter.entity_ids if runtime.exporter is not None else None,
        "occupancy": _coordinator_info(runtime.occupancy),
        "usage_counters": _coordinator_info(runtime.usage_counters),
        "usage_counter_reports": (
            {
                "mapping": runtime.usage_reporter.mapping,
                "last_sent": {k: v.isoformat() for k, v in runtime.usage_reporter.last_sent.items()},
                "last_value": runtime.usage_reporter.last_value,
                "stopped": sorted(runtime.usage_reporter.stopped_counters),
            }
            if runtime.usage_reporter is not None
            else None
        ),
        "displays": runtime.display_sender.diagnostics() if runtime.display_sender is not None else None,
    }
    return async_redact_data(result, TO_REDACT)
