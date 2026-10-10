"""Occupancy binary sensors per bookable (opt-in: "Occupancy sensors").

* ``occupied``: a booking runs now,
* ``pre_start``: now within the lead time before the next booking (the
  bookable's ``automation_pre_start_minutes`` in the ERP),
* ``open``: the bookable (or its location) is open now - only for bookables
  with opening hours.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .occupancy import OccupancyCoordinator, bookable_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the sensors of every bookable the occupancy answer lists, now and later."""
    coordinator: OccupancyCoordinator | None = entry.runtime_data.occupancy
    if coordinator is None:
        return
    known: set[tuple[str, str]] = set()

    @callback
    def add_new() -> None:
        new: list[BinarySensorEntity] = []
        for bookable_id, item in (coordinator.data or {}).items():
            kinds = ["occupied", "pre_start"]
            if isinstance(item.get("opening_hours"), dict):
                kinds.append("open")
            for kind in kinds:
                if (bookable_id, kind) not in known:
                    known.add((bookable_id, kind))
                    new.append(OccupancyBinarySensor(coordinator, entry, bookable_id, kind))
        if new:
            async_add_entities(new)

    add_new()
    entry.async_on_unload(coordinator.async_add_listener(add_new))


def booking_attributes(booking: dict[str, Any] | None, prefix: str = "") -> dict[str, Any]:
    """Title, start, end and participants of a booking from the occupancy answer."""
    booking = booking or {}
    return {
        f"{prefix}booking_id": booking.get("id"),
        f"{prefix}title": booking.get("title"),
        f"{prefix}start": booking.get("start"),
        f"{prefix}end": booking.get("end"),
        f"{prefix}participants": booking.get("participants"),
    }


class OccupancyBinarySensor(CoordinatorEntity[OccupancyCoordinator], BinarySensorEntity):
    """One occupancy aspect of a bookable."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: OccupancyCoordinator, entry, bookable_id: str, kind: str) -> None:
        super().__init__(coordinator)
        self._bookable_id = bookable_id
        self._kind = kind
        self._attr_unique_id = f"{entry.entry_id}-bookable-{bookable_id}-{kind}"
        self._attr_translation_key = kind
        if kind == "occupied":
            self._attr_device_class = BinarySensorDeviceClass.OCCUPANCY
        item = (coordinator.data or {}).get(bookable_id, {})
        name = item.get("name") or entry.runtime_data.bookable_names.get(bookable_id)
        self._attr_device_info = bookable_device_info(entry.entry_id, bookable_id, name)

    @property
    def _item(self) -> dict[str, Any] | None:
        return (self.coordinator.data or {}).get(self._bookable_id)

    @property
    def available(self) -> bool:
        item = self._item
        if item is None:
            return False
        if self._kind == "open" and not isinstance(item.get("opening_hours"), dict):
            return False
        return super().available

    @property
    def is_on(self) -> bool | None:
        item = self._item
        if item is None:
            return None
        if self._kind == "occupied":
            return bool(item.get("occupied"))
        if self._kind == "pre_start":
            return bool(item.get("pre_start_active"))
        hours = item.get("opening_hours")
        return bool(hours.get("open_now")) if isinstance(hours, dict) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        item = self._item or {}
        if self._kind == "occupied":
            return {
                **booking_attributes(item.get("current_booking")),
                "post_end_active": item.get("post_end_active"),
            }
        if self._kind == "pre_start":
            return {
                "pre_start_minutes": item.get("pre_start_minutes"),
                **booking_attributes(item.get("next_booking"), "next_"),
            }
        hours = item.get("opening_hours") or {}
        return {"next_open": hours.get("next_open"), "next_close": hours.get("next_close")}
