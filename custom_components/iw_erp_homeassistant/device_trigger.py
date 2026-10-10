"""Device triggers on the ERP connection's device: access and check-in events.

The device "inwendo ERP <host>" (created with the "last access" sensor when the
option "Access and check-in events" is on) offers four triggers in the
automation editor; they listen to the ``iw_erp_access`` / ``iw_erp_check_in``
events of that config entry.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_EVENT_DATA, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, EVENT_ACCESS, EVENT_CHECK_IN

# trigger type -> (event type, event data that must match besides the config entry)
TRIGGERS: dict[str, tuple[str, dict[str, Any]]] = {
    "access_granted": (EVENT_ACCESS, {"success": True}),
    "access_denied": (EVENT_ACCESS, {"success": False}),
    "checked_in": (EVENT_CHECK_IN, {"checked_in": True}),
    "checked_out": (EVENT_CHECK_IN, {"checked_in": False}),
}

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend({vol.Required(CONF_TYPE): vol.In(TRIGGERS)})


def _entry_id_of_hub(hass: HomeAssistant, device_id: str) -> str | None:
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for domain, identifier in device.identifiers:
        if domain == DOMAIN and identifier in device.config_entries:
            return identifier
    return None


async def async_get_triggers(hass: HomeAssistant, device_id: str) -> list[dict[str, Any]]:
    """The triggers of the ERP connection's device (none for bookable devices)."""
    if _entry_id_of_hub(hass, device_id) is None:
        return []
    return [
        {CONF_PLATFORM: "device", CONF_DOMAIN: DOMAIN, CONF_DEVICE_ID: device_id, CONF_TYPE: trigger_type}
        for trigger_type in TRIGGERS
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Listen to the event behind a device trigger."""
    event_type, match = TRIGGERS[config[CONF_TYPE]]
    entry_id = _entry_id_of_hub(hass, config[CONF_DEVICE_ID])
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: event_type,
            CONF_EVENT_DATA: {**match, "config_entry_id": entry_id},
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
