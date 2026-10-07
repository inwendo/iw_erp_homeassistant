"""Config flow for inwendo ERP / vynst integration."""
from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ERR_INVALID_RESPONSE, ApiError, api_get_json, sanitize_url
from .const import (
    CONF_DISPLAYS,
    CONF_EXPOSE_HA_LOCKS,
    CONF_HOST,
    CONF_IMPORT_ERP_LOCKS,
    CONF_LOCK_SECRET,
    CONF_TOKEN,
    DOMAIN,
    OPENDISPLAY_DOMAIN,
)
from .display import async_fetch_erp_displays, configured_displays

_LOGGER = logging.getLogger(__name__)

# Empty values used as description_placeholders on the first form render;
# they avoid literal ``{last_error}`` leaking into the UI.
_EMPTY_PLACEHOLDERS = {
    "last_error": "",
    "error_code": "",
    "error_detail": "",
}


async def _validate(hass, host: str, token: str) -> ApiError | str | None:
    """Check host + API key against the bookables endpoint.

    Returns ``None`` when valid, an :class:`ApiError` or an error key otherwise.
    """
    session = async_get_clientsession(hass)
    url = f"{host}/api/homeassistant/bookables"
    # Log a sanitized URL so any credentials the user embedded in the
    # host (e.g. ``https://user:pass@host``) never hit the log file.
    _LOGGER.debug("Attempting to connect to %s", sanitize_url(url))

    data, error = await api_get_json(
        session,
        url,
        token,
        _LOGGER,
        operation="Validate ERP credentials",
        timeout=10,
    )
    if error:
        return error
    if not isinstance(data, list):
        _LOGGER.error(
            "Validate ERP credentials failed: key=%s url=%s "
            "reason=unexpected_response_shape type=%s",
            ERR_INVALID_RESPONSE,
            sanitize_url(url),
            type(data).__name__,
        )
        return ERR_INVALID_RESPONSE
    return None


def _apply_error(result, errors: dict[str, str]) -> dict[str, str]:
    if isinstance(result, ApiError):
        errors["base"] = result.key
        return {**_EMPTY_PLACEHOLDERS, **result.placeholders()}
    errors["base"] = result
    return dict(_EMPTY_PLACEHOLDERS)


class ERPCalendarConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ERP Calendar Sync."""

    VERSION = 1
    MINOR_VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options: opt-in lock import / export."""
        return IwErpOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        placeholders = dict(_EMPTY_PLACEHOLDERS)

        if user_input is not None:
            host = user_input[CONF_HOST].rstrip('/')
            token = user_input[CONF_TOKEN]

            result = await _validate(self.hass, host, token)
            if result is not None:
                placeholders = _apply_error(result, errors)
            else:
                _LOGGER.info(
                    "Successfully connected to ERP API at %s", sanitize_url(host)
                )
                await self.async_set_unique_id(host)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=host,
                    data={
                        CONF_HOST: host,
                        CONF_TOKEN: token,
                        CONF_LOCK_SECRET: secrets.token_hex(32),
                    },
                )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default="https://"): str,
                vol.Required(CONF_TOKEN): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The ERP rejected the API key: ask for a new one."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate and store a new API key."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        placeholders = dict(_EMPTY_PLACEHOLDERS)

        if user_input is not None:
            result = await _validate(self.hass, entry.data[CONF_HOST], user_input[CONF_TOKEN])
            if result is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_TOKEN: user_input[CONF_TOKEN]}
                )
            placeholders = _apply_error(result, errors)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): str}),
            errors=errors,
            description_placeholders={**placeholders, "host": entry.data[CONF_HOST]},
        )


def _display_label(display: dict[str, Any]) -> str:
    """Form field of one ERP room display: its name, the room, and the id to map back."""
    name = str(display.get("name") or "").strip() or "Display"
    target = str(display.get("target") or "").strip()
    label = f"{name} – {target}" if target and target != name else name
    return f"{label} (#{display['id']})"


class IwErpOptionsFlow(OptionsFlow):
    """Opt-in lock synchronisation in both directions, and room displays."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._erp_displays: list[dict[str, Any]] = []

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._options = {
                CONF_IMPORT_ERP_LOCKS: bool(user_input.get(CONF_IMPORT_ERP_LOCKS, False)),
                CONF_EXPOSE_HA_LOCKS: list(user_input.get(CONF_EXPOSE_HA_LOCKS, [])),
            }
            # Room displays only when there is something to assign on both sides:
            # OpenDisplay displays in the ERP and the OpenDisplay integration here.
            if self.hass.config_entries.async_entries(OPENDISPLAY_DOMAIN):
                displays = await async_fetch_erp_displays(
                    self.hass,
                    self.config_entry.data[CONF_HOST],
                    self.config_entry.data[CONF_TOKEN],
                )
                if displays:
                    self._erp_displays = displays
                    return await self.async_step_displays()
            # Keep an existing assignment when the ERP is just not reachable right now.
            current = configured_displays(self.config_entry.options)
            if current:
                self._options[CONF_DISPLAYS] = current
            return self.async_create_entry(data=self._options)

        # The ERP locks imported by this integration can not be offered back to the ERP.
        own_locks = [
            reg.entity_id
            for reg in er.async_entries_for_config_entry(
                er.async_get(self.hass), self.config_entry.entry_id
            )
            if reg.domain == "lock"
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_IMPORT_ERP_LOCKS, default=False): bool,
                vol.Optional(CONF_EXPOSE_HA_LOCKS, default=[]): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="lock", multiple=True, exclude_entities=own_locks
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, self.config_entry.options),
        )

    async def async_step_displays(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Assign an OpenDisplay panel to each ERP room display (all optional)."""
        labels = {_display_label(d): str(d["id"]) for d in self._erp_displays}

        if user_input is not None:
            mapping = {
                labels[label]: device_id
                for label, device_id in user_input.items()
                if label in labels and device_id
            }
            return self.async_create_entry(data={**self._options, CONF_DISPLAYS: mapping})

        current = configured_displays(self.config_entry.options)
        fields: dict[Any, Any] = {}
        for label, display_id in labels.items():
            key = (
                vol.Optional(label, description={"suggested_value": current[display_id]})
                if display_id in current
                else vol.Optional(label)
            )
            fields[key] = selector.DeviceSelector(
                selector.DeviceSelectorConfig(integration=OPENDISPLAY_DOMAIN)
            )
        return self.async_show_form(step_id="displays", data_schema=vol.Schema(fields))
