"""Config flow for inwendo ERP / vynst integration."""
import logging
import voluptuous as vol
import aiohttp

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, CONF_HOST, CONF_TOKEN

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
            
            # Test the connection and credentials
            session = async_get_clientsession(self.hass)
            try:
                headers = {"x-session-token": f"{token}"}
                url = f"{host}/api/event/base_bookable"
                _LOGGER.debug(f"Attempting to connect to {url}")
                
                async with session.get(url, headers=headers, timeout=10) as response:
                    response.raise_for_status()
                    # We could also check the content here if needed
                    _LOGGER.info("Successfully connected to ERP API")

                    # Use host as the unique ID for this config entry
                    await self.async_set_unique_id(host)
                    self._abort_if_unique_id_configured()

                    return self.async_create_entry(
                        title=host, 
                        data={
                            CONF_HOST: host,
                            CONF_TOKEN: token
                        }
                    )
            except aiohttp.ClientError:
                _LOGGER.error("Failed to connect to ERP host: %s", host)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("An unknown error occurred during API validation")
                errors["base"] = "unknown"

        data_schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default="https://"): str,
                vol.Required(CONF_TOKEN): str,
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )

