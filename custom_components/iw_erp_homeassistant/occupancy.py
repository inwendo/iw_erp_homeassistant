"""Occupancy of the bookables (opt-in: "Occupancy sensors").

Polls ``/api/homeassistant/occupancy`` every minute (the answer changes with
the clock: a booking starts, the lead time begins) and right after a booking
webhook. The sensors in ``binary_sensor.py`` / ``sensor.py`` read from here.
When the ERP has the function switched off the coordinator holds no
bookables, so no entities appear, and it asks again every 30 minutes.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import api_get_json, is_auth_rejected
from .issues import async_api_key_rejected
from .const import CONF_HOST, CONF_TOKEN, DOMAIN, FEATURE_OCCUPANCY

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=60)
# While the ERP has the function switched off: look again now and then.
DISABLED_INTERVAL = timedelta(minutes=30)


def bookable_device_info(entry_id: str, bookable_id: str, name: str | None) -> DeviceInfo:
    """The device of a bookable: its calendar, occupancy and usage counter entities."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}-bookable-{bookable_id}")},
        name=name or f"Bookable {bookable_id}",
        manufacturer="inwendo",
        model="Bookable",
    )


def bookable_id_from_device(device) -> str | None:
    """The ERP bookable id of a device created by :func:`bookable_device_info`."""
    for domain, identifier in device.identifiers:
        if domain == DOMAIN and "-bookable-" in identifier:
            return identifier.rsplit("-bookable-", 1)[1]
    return None


class OccupancyCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Bookable id -> occupancy record as the ERP sends it."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_occupancy",
            update_interval=SCAN_INTERVAL,
        )
        self.host: str = entry.data[CONF_HOST]
        self.token: str = entry.data[CONF_TOKEN]
        self.features = entry.runtime_data.features
        self.generated_at: str | None = None

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        data, error = await api_get_json(
            async_get_clientsession(self.hass),
            f"{self.host}/api/homeassistant/occupancy",
            self.token,
            _LOGGER,
            "Fetch ERP occupancy",
            timeout=15,
        )
        if error is not None:
            if self.features.handle_error(FEATURE_OCCUPANCY, error):
                self.update_interval = DISABLED_INTERVAL
                return {}
            if is_auth_rejected(error):
                async_api_key_rejected(self.hass, self.config_entry)
                raise ConfigEntryAuthFailed(f"Cannot fetch occupancy ({error.key})")
            raise UpdateFailed(f"Cannot fetch occupancy ({error.key})")
        if not isinstance(data, dict) or not isinstance(data.get("bookables"), list):
            raise UpdateFailed("Unexpected occupancy answer")

        self.features.ok(FEATURE_OCCUPANCY)
        self.update_interval = SCAN_INTERVAL
        self.generated_at = data.get("generated_at")
        return {
            str(item["id"]): item
            for item in data["bookables"]
            if isinstance(item, dict) and item.get("id") is not None
        }
