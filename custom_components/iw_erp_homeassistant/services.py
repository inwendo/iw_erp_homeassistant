"""Services of the integration (always registered, no option needed).

``iw_erp_homeassistant.check_access``
    Asks the ERP whether a credential (RFID card UID, QR code, keypad code)
    opens a smart lock: ``POST /api/homeassistant/check_access``. Lets an
    automation with a card or code reader use the ERP's access rules; the ERP
    writes its smart lock log. Returns the ERP's answer.

``iw_erp_homeassistant.create_ticket``
    Creates an ERP ticket (water alarm, broken heating, ...):
    ``POST /api/homeassistant/ticket``. With a ``dedupe_key`` the ERP creates
    no second ticket while one with the same key is open.

Both functions can be switched off in the ERP; the services then fail with a
clear error instead of an HTTP status.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import instance_id
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ApiError, api_request_json, is_auth_rejected, is_feature_disabled, is_missing_scope
from .const import (
    CONF_HOST,
    CONF_TOKEN,
    DOMAIN,
    FEATURE_CHECK_ACCESS,
    FEATURE_SCOPES,
    FEATURE_SETTINGS,
    FEATURE_TICKET,
)
from .issues import async_api_key_rejected
from .occupancy import bookable_id_from_device

_LOGGER = logging.getLogger(__name__)

SERVICE_CHECK_ACCESS = "check_access"
SERVICE_CREATE_TICKET = "create_ticket"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_CREDENTIAL = "credential"
ATTR_CREDENTIAL_TYPE = "credential_type"
ATTR_SMART_LOCK_ID = "smart_lock_id"
ATTR_LOCK = "lock"
ATTR_OPEN = "open"
ATTR_TITLE = "title"
ATTR_DESCRIPTION = "description"
ATTR_BOOKABLE = "bookable"
ATTR_BOOKABLE_ID = "bookable_id"
ATTR_PRIORITY = "priority"
ATTR_DEDUPE_KEY = "dedupe_key"

CREDENTIAL_TYPES = ["auto", "access_code", "member_card", "card"]
PRIORITIES = ["low", "normal", "high", "urgent"]

CHECK_ACCESS_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
            vol.Required(ATTR_CREDENTIAL): vol.All(cv.string, vol.Length(min=1, max=2048)),
            vol.Optional(ATTR_CREDENTIAL_TYPE, default="auto"): vol.In(CREDENTIAL_TYPES),
            vol.Optional(ATTR_SMART_LOCK_ID): vol.All(vol.Coerce(int), vol.Range(min=1)),
            vol.Optional(ATTR_LOCK): cv.entity_id,
            vol.Optional(ATTR_OPEN, default=False): cv.boolean,
        }
    ),
    cv.has_at_least_one_key(ATTR_SMART_LOCK_ID, ATTR_LOCK),
)

CREATE_TICKET_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_TITLE): vol.All(cv.string, vol.Length(min=1, max=255)),
        vol.Optional(ATTR_DESCRIPTION): cv.string,
        vol.Optional(ATTR_BOOKABLE): cv.string,
        vol.Optional(ATTR_BOOKABLE_ID): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Optional(ATTR_PRIORITY): vol.In(PRIORITIES),
        vol.Optional(ATTR_DEDUPE_KEY): vol.All(cv.string, vol.Length(min=1, max=190)),
    }
)


def _loaded_entry(hass: HomeAssistant, entry_id: str | None):
    """The config entry a call is for: the given one, or the only loaded one."""
    loaded = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if entry_id:
        for entry in loaded:
            if entry.entry_id == entry_id:
                return entry
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="entry_not_loaded", translation_placeholders={"entry_id": entry_id}
        )
    if len(loaded) == 1:
        return loaded[0]
    raise ServiceValidationError(
        translation_domain=DOMAIN, translation_key="entry_required" if loaded else "no_entry"
    )


def _raise_for(hass: HomeAssistant, entry, feature: str, error: ApiError) -> None:
    """Turn a failed ERP request into an understandable service error."""
    if is_feature_disabled(error):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="feature_disabled",
            translation_placeholders={"feature": feature, "setting": FEATURE_SETTINGS.get(feature, "-")},
        )
    if is_missing_scope(error):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="missing_scope",
            translation_placeholders={"feature": feature, "scope": FEATURE_SCOPES.get(feature, "-")},
        )
    if is_auth_rejected(error):
        async_api_key_rejected(hass, entry)
        entry.async_start_reauth(hass)
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="api_key_rejected")
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="request_failed",
        translation_placeholders={"error": error.erp_detail or error.erp_code or error.key},
    )


async def _post(hass: HomeAssistant, entry, path: str, body: dict[str, Any], operation: str, feature: str) -> dict[str, Any]:
    data, error = await api_request_json(
        async_get_clientsession(hass),
        "POST",
        f"{entry.data[CONF_HOST]}{path}",
        entry.data[CONF_TOKEN],
        _LOGGER,
        operation,
        json_body=body,
        timeout=30,
        level=logging.WARNING,
    )
    if error is not None:
        _raise_for(hass, entry, feature, error)
    if not isinstance(data, dict):
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="request_failed", translation_placeholders={"error": "invalid_response"}
        )
    entry.runtime_data.features.ok(feature)
    return data


def _lock_target(hass: HomeAssistant, entity_id: str) -> tuple[str | None, dict[str, Any]]:
    """(config entry id or None, body part) for a lock entity.

    An ERP lock imported by this integration is addressed by its smart lock id;
    any other Home Assistant lock by this instance's id and its entity id (it
    has to be offered to the ERP and synced there).
    """
    reg = er.async_get(hass).async_get(entity_id)
    if reg is not None and reg.platform == DOMAIN and reg.unique_id and "-smart-lock-" in reg.unique_id:
        return reg.config_entry_id, {ATTR_SMART_LOCK_ID: int(reg.unique_id.rsplit("-smart-lock-", 1)[1])}
    return None, {"lock_id": entity_id}


async def async_check_access(call: ServiceCall) -> ServiceResponse:
    """Handle iw_erp_homeassistant.check_access."""
    hass = call.hass
    body: dict[str, Any] = {
        ATTR_CREDENTIAL: call.data[ATTR_CREDENTIAL],
        ATTR_CREDENTIAL_TYPE: call.data[ATTR_CREDENTIAL_TYPE],
    }
    entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
    if ATTR_SMART_LOCK_ID in call.data:
        body[ATTR_SMART_LOCK_ID] = call.data[ATTR_SMART_LOCK_ID]
    else:
        lock_entry_id, part = _lock_target(hass, call.data[ATTR_LOCK])
        body.update(part)
        if "lock_id" in part:
            body["instance_id"] = await instance_id.async_get(hass)
        entry_id = entry_id or lock_entry_id
    if call.data[ATTR_OPEN]:
        body[ATTR_OPEN] = True
    entry = _loaded_entry(hass, entry_id)
    result = await _post(hass, entry, "/api/homeassistant/check_access", body, "Check access in ERP", FEATURE_CHECK_ACCESS)
    _LOGGER.debug("ERP access check: granted=%s reason=%s", result.get("granted"), result.get("reason"))
    return result


async def async_create_ticket(call: ServiceCall) -> ServiceResponse:
    """Handle iw_erp_homeassistant.create_ticket."""
    hass = call.hass
    entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
    body: dict[str, Any] = {ATTR_TITLE: call.data[ATTR_TITLE]}
    for key in (ATTR_DESCRIPTION, ATTR_PRIORITY, ATTR_DEDUPE_KEY):
        if call.data.get(key):
            body[key] = call.data[key]
    if ATTR_BOOKABLE_ID in call.data:
        body[ATTR_BOOKABLE_ID] = call.data[ATTR_BOOKABLE_ID]
    elif call.data.get(ATTR_BOOKABLE):
        device = dr.async_get(hass).async_get(call.data[ATTR_BOOKABLE])
        bookable_id = bookable_id_from_device(device) if device is not None else None
        if bookable_id is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_a_bookable")
        body[ATTR_BOOKABLE_ID] = int(bookable_id)
        if not entry_id:
            entry_id = next(
                (eid for eid in device.config_entries if (e := hass.config_entries.async_get_entry(eid)) and e.domain == DOMAIN),
                None,
            )
    entry = _loaded_entry(hass, entry_id)
    return await _post(hass, entry, "/api/homeassistant/ticket", body, "Create ERP ticket", FEATURE_TICKET)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the services (once, in async_setup)."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_CHECK_ACCESS,
        async_check_access,
        schema=CHECK_ACCESS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CREATE_TICKET,
        async_create_ticket,
        schema=CREATE_TICKET_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
