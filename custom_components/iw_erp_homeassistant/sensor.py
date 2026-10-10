"""Sensors of the integration, each group opt-in.

* Occupancy (option "Occupancy sensors"): per bookable the next booking
  (timestamp; title and participants as attributes) and the participants of the
  current booking.
* Usage counters (option "Usage counters"): every ERP usage counter, read-only.
* Last access (option "Access and check-in events"): one per config entry and,
  with the ERP lock import on, one per imported smart lock; fed by the
  ``iw_erp_access`` events and restored after a restart.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import sanitize_url
from .binary_sensor import booking_attributes
from .const import CONF_ACCESS_EVENTS, CONF_HOST, DOMAIN, SIGNAL_ACCESS
from .occupancy import OccupancyCoordinator, bookable_device_info
from .usage_counters import UsageCounterCoordinator

# Units the ERP uses for energy meters.
ENERGY_UNITS = {"kWh", "Wh", "MWh"}


def hub_device_info(entry) -> DeviceInfo:
    """The device of the ERP connection itself (last access, device triggers)."""
    host = urlparse(sanitize_url(entry.data[CONF_HOST])).netloc or entry.data[CONF_HOST]
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"inwendo ERP {host}",
        manufacturer="inwendo",
        model="ERP",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=sanitize_url(entry.data[CONF_HOST]),
    )


def _parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_util.get_default_time_zone())


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor groups that are switched on."""
    data = entry.runtime_data

    occupancy: OccupancyCoordinator | None = data.occupancy
    if occupancy is not None:
        known_bookables: set[str] = set()

        @callback
        def add_bookables() -> None:
            new = [b for b in (occupancy.data or {}) if b not in known_bookables]
            known_bookables.update(new)
            entities: list[SensorEntity] = []
            for bookable_id in new:
                entities.append(NextBookingSensor(occupancy, entry, bookable_id))
                entities.append(ParticipantsSensor(occupancy, entry, bookable_id))
            if entities:
                async_add_entities(entities)

        add_bookables()
        entry.async_on_unload(occupancy.async_add_listener(add_bookables))

    counters: UsageCounterCoordinator | None = data.usage_counters
    if counters is not None:
        known_counters: set[str] = set()

        @callback
        def add_counters() -> None:
            new = [c for c in (counters.data or {}) if c not in known_counters]
            known_counters.update(new)
            if new:
                async_add_entities(UsageCounterSensor(counters, entry, c) for c in new)

        add_counters()
        entry.async_on_unload(counters.async_add_listener(add_counters))

    if entry.options.get(CONF_ACCESS_EVENTS):
        async_add_entities([LastAccessSensor(entry, None, None)])
        locks = data.lock_coordinator
        if locks is not None:
            known_locks: set[str] = set()

            @callback
            def add_locks() -> None:
                new = [lock_id for lock_id in (locks.data or {}) if lock_id not in known_locks]
                known_locks.update(new)
                if new:
                    async_add_entities(
                        LastAccessSensor(entry, lock_id, locks.data[lock_id]["name"]) for lock_id in new
                    )

            add_locks()
            entry.async_on_unload(locks.async_add_listener(add_locks))


class _BookableSensor(CoordinatorEntity[OccupancyCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _kind = ""

    def __init__(self, coordinator: OccupancyCoordinator, entry, bookable_id: str) -> None:
        super().__init__(coordinator)
        self._bookable_id = bookable_id
        self._attr_unique_id = f"{entry.entry_id}-bookable-{bookable_id}-{self._kind}"
        self._attr_translation_key = self._kind
        item = (coordinator.data or {}).get(bookable_id, {})
        name = item.get("name") or entry.runtime_data.bookable_names.get(bookable_id)
        self._attr_device_info = bookable_device_info(entry.entry_id, bookable_id, name)

    @property
    def _item(self) -> dict[str, Any] | None:
        return (self.coordinator.data or {}).get(self._bookable_id)

    @property
    def available(self) -> bool:
        return super().available and self._item is not None


class NextBookingSensor(_BookableSensor):
    """Start of the next booking (within the next 14 days)."""

    _kind = "next_booking"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        booking = (self._item or {}).get("next_booking") or {}
        return _parse_time(booking.get("start"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return booking_attributes((self._item or {}).get("next_booking"))


class ParticipantsSensor(_BookableSensor):
    """Participants of the booking running now (unknown without one)."""

    _kind = "participants"
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int | None:
        booking = (self._item or {}).get("current_booking") or {}
        value = booking.get("participants")
        return value if isinstance(value, (int, float)) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return booking_attributes((self._item or {}).get("current_booking"))


class UsageCounterSensor(CoordinatorEntity[UsageCounterCoordinator], SensorEntity):
    """An ERP usage counter (meter reading), read-only."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: UsageCounterCoordinator, entry, counter_id: str) -> None:
        super().__init__(coordinator)
        self._counter_id = counter_id
        self._attr_unique_id = f"{entry.entry_id}-usage-counter-{counter_id}"
        counter = coordinator.data[counter_id]
        self._attr_name = counter.get("name") or f"Usage counter {counter_id}"
        self._attr_native_unit_of_measurement = counter.get("unit") or None
        if self._attr_native_unit_of_measurement in ENERGY_UNITS:
            self._attr_device_class = SensorDeviceClass.ENERGY
        if counter.get("bookable_id") is not None:
            self._attr_device_info = bookable_device_info(
                entry.entry_id, str(counter["bookable_id"]), counter.get("bookable_name")
            )

    @property
    def _counter(self) -> dict[str, Any] | None:
        return (self.coordinator.data or {}).get(self._counter_id)

    @property
    def available(self) -> bool:
        return super().available and self._counter is not None

    @property
    def native_value(self) -> float | None:
        value = (self._counter or {}).get("value")
        return value if isinstance(value, (int, float)) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        counter = self._counter or {}
        return {
            "erp_usage_counter_id": self._counter_id,
            "bookable_id": counter.get("bookable_id"),
            "bookable": counter.get("bookable_name"),
            "use_real_usage_data": counter.get("use_real_usage_data"),
            "updated": counter.get("updated"),
        }


class LastAccessSensor(RestoreSensor):
    """Time of the last ERP smart lock access, with who, where, what and whether it worked."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_should_poll = False

    def __init__(self, entry, lock_id: str | None, lock_name: str | None) -> None:
        self._entry_id = entry.entry_id
        self._lock_id = lock_id
        self._attr_native_value: datetime | None = None
        self._attr_extra_state_attributes = {}
        if lock_id is None:
            self._attr_has_entity_name = True
            self._attr_translation_key = "last_access"
            self._attr_unique_id = f"{entry.entry_id}-last-access"
            self._attr_device_info = hub_device_info(entry)
        else:
            self._attr_name = f"{lock_name or f'Smart lock {lock_id}'} last access"
            self._attr_unique_id = f"{entry.entry_id}-smart-lock-{lock_id}-last-access"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        last_data = await self.async_get_last_sensor_data()
        if last_data is not None and isinstance(last_data.native_value, datetime):
            self._attr_native_value = last_data.native_value
        if last_state is not None:
            self._attr_extra_state_attributes = {
                key: last_state.attributes.get(key)
                for key in ("person", "person_id", "lock", "smart_lock_id", "action", "success", "source")
            }
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_ACCESS, self._access))

    @callback
    def _access(self, entry_id: str, data: dict[str, Any]) -> None:
        if entry_id != self._entry_id:
            return
        if self._lock_id is not None and str(data.get("smart_lock_id")) != self._lock_id:
            return
        self._attr_native_value = _parse_time(data.get("at")) or dt_util.utcnow()
        self._attr_extra_state_attributes = {
            "person": data.get("person_name"),
            "person_id": data.get("person_id"),
            "lock": data.get("smart_lock_name"),
            "smart_lock_id": data.get("smart_lock_id"),
            "action": data.get("action"),
            "success": data.get("success"),
            "source": data.get("source"),
        }
        self.async_write_ha_state()
