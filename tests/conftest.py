"""Shared fixtures."""
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ in every test."""
    yield


@pytest.fixture
async def external_url(hass: HomeAssistant) -> None:
    """Home Assistant with an external https URL (the ERP accepts its webhook)."""
    await async_process_ha_core_config(hass, {"external_url": "https://ha.example.com"})
