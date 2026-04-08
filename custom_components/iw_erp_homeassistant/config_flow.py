"""Config flow for inwendo ERP / vynst integration."""
import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ERR_INVALID_RESPONSE, api_get_json
from .const import CONF_HOST, CONF_TOKEN, DOMAIN

_LOGGER = logging.getLogger(__name__)


class ERPCalendarConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ERP Calendar Sync."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            host = user_input[CONF_HOST].rstrip('/')
            token = user_input[CONF_TOKEN]

            session = async_get_clientsession(self.hass)
            url = f"{host}/api/homeassistant/bookables"
            _LOGGER.debug("Attempting to connect to %s", url)

            data, err_key = await api_get_json(
                session,
                url,
                token,
                _LOGGER,
                operation="Validate ERP credentials",
                timeout=10,
            )

            if err_key:
                errors["base"] = err_key
            elif not isinstance(data, list):
                _LOGGER.error(
                    "Validate ERP credentials failed: key=%s url=%s "
                    "reason=unexpected_response_shape type=%s",
                    ERR_INVALID_RESPONSE,
                    url,
                    type(data).__name__,
                )
                errors["base"] = ERR_INVALID_RESPONSE
            else:
                _LOGGER.info("Successfully connected to ERP API at %s", host)
                await self.async_set_unique_id(host)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=host,
                    data={
                        CONF_HOST: host,
                        CONF_TOKEN: token,
                    },
                )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default="https://"): str,
                vol.Required(CONF_TOKEN): str,
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )
