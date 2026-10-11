"""The automation blueprints in blueprints/automation/iw_erp, run against an ERP calendar."""
from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.components import automation
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .test_integration import make_entry, mock_erp

BLUEPRINTS = Path(__file__).parent.parent / "blueprints" / "automation" / "iw_erp"
# tests/const.ICAL: one booking 2030-01-01 10:00-11:00 UTC on bookable 1 ("Room 1")
START = dt_util.parse_datetime("2030-01-01T10:00:00+00:00")


@pytest.fixture
async def room(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tmp_path, freezer):
    """calendar.room_1 with one booking, the blueprints in the config folder, clock at 08:50."""
    hass.config.config_dir = str(tmp_path)
    shutil.copytree(BLUEPRINTS, tmp_path / "blueprints" / "automation" / "iw_erp")
    freezer.move_to(START - timedelta(minutes=70))
    mock_erp(aioclient_mock, bookables=[{"id": 1, "name": "Room 1"}])
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert await async_setup_component(hass, "homeassistant", {})
    await hass.async_block_till_done()
    assert hass.states.get("calendar.room_1") is not None
    yield freezer
    # a calendar trigger refreshes on a timer: detach the automations before the test ends
    if hass.services.has_service(automation.DOMAIN, "turn_off"):
        await hass.services.async_call(automation.DOMAIN, "turn_off", {"entity_id": "all"}, blocking=True)
        await hass.async_block_till_done()


async def advance_to(hass: HomeAssistant, freezer, target) -> None:
    """Move the clock in small steps so the calendar trigger sees every boundary."""
    while dt_util.utcnow() < target:
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def use_blueprint(hass: HomeAssistant, path: str, inputs: dict) -> None:
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {automation.DOMAIN: {"id": path, "use_blueprint": {"path": f"iw_erp/{path}", "input": inputs}}},
    )
    await hass.async_block_till_done()
    states = hass.states.async_all("automation")
    assert len(states) == 1 and states[0].state == "on", f"{path} did not load"


async def test_preheat_room(hass: HomeAssistant, room) -> None:
    calls = async_mock_service(hass, "climate", "set_temperature")
    await use_blueprint(
        hass,
        "preheat_room.yaml",
        {
            "calendar": "calendar.room_1",
            "climate": {"entity_id": "climate.room_1"},
            "lead_time": {"hours": 0, "minutes": -30, "seconds": 0},
            "comfort_temperature": 21,
            "eco_temperature": 16,
        },
    )
    await advance_to(hass, room, START - timedelta(minutes=31))
    assert not calls
    await advance_to(hass, room, START - timedelta(minutes=28))
    assert [c.data["temperature"] for c in calls] == [21]
    await advance_to(hass, room, START + timedelta(minutes=62))
    assert [c.data["temperature"] for c in calls] == [21, 16]


async def test_booking_light(hass: HomeAssistant, room) -> None:
    on = async_mock_service(hass, "light", "turn_on")
    off = async_mock_service(hass, "light", "turn_off")
    hass.states.async_set("light.room_1", "off")
    await use_blueprint(
        hass,
        "booking_light.yaml",
        {"calendar": "calendar.room_1", "lights": {"entity_id": "light.room_1"},
         "off_delay": {"hours": 0, "minutes": 10, "seconds": 0}},
    )
    await advance_to(hass, room, START + timedelta(minutes=2))
    assert len(on) == 1 and not off
    await advance_to(hass, room, START + timedelta(minutes=68))
    assert not off
    await advance_to(hass, room, START + timedelta(minutes=72))
    assert len(off) == 1


async def test_frost_protection(hass: HomeAssistant, room) -> None:
    calls = async_mock_service(hass, "climate", "set_temperature")
    hass.states.async_set("binary_sensor.room_1_open", "on")
    await use_blueprint(
        hass,
        "frost_protection.yaml",
        {"calendar": "calendar.room_1", "climate": {"entity_id": "climate.room_1"},
         "opening_hours": "binary_sensor.room_1_open", "frost_temperature": 7},
    )
    # Opening hours end, but a booking starts within the quiet window: no frost protection.
    hass.states.async_set("binary_sensor.room_1_open", "off")
    await hass.async_block_till_done()
    assert not calls
    # 15 minutes after the booking, nothing follows: frost protection.
    await advance_to(hass, room, START + timedelta(minutes=77))
    assert [c.data["temperature"] for c in calls] == [7]
