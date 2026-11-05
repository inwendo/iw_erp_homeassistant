"""The occupancy based blueprints (occupancy_heating.yaml, occupancy_light.yaml).

They read the occupancy sensors of the integration ("Occupied", "Next booking"); the tests set
those states directly, like the occupancy coordinator does after each poll or booking webhook.
Unlike the calendar trigger, which scans 15 minute windows once, they follow a booking made at
short notice or a cancelled booking at once (found with a real Home Assistant, ERP repository
e2e/tests/api/homeassistant-runtime.spec.ts).
"""
from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from homeassistant.components import automation
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

BLUEPRINTS = Path(__file__).parent.parent / "blueprints" / "automation" / "iw_erp"
START = dt_util.parse_datetime("2030-01-01T10:00:00+00:00")
OCCUPIED = "binary_sensor.room_1_occupied"
NEXT = "sensor.room_1_next_booking"


@pytest.fixture
async def room(hass: HomeAssistant, tmp_path, freezer):
    """An empty room with its next booking at START, clock at START - 70 minutes."""
    hass.config.config_dir = str(tmp_path)
    shutil.copytree(BLUEPRINTS, tmp_path / "blueprints" / "automation" / "iw_erp")
    freezer.move_to(START - timedelta(minutes=70))
    assert await async_setup_component(hass, "homeassistant", {})
    hass.states.async_set(OCCUPIED, "off", {"device_class": "occupancy"})
    hass.states.async_set(NEXT, START.isoformat(), {"device_class": "timestamp"})
    hass.states.async_set("light.room_1", "off")
    hass.states.async_set("sun.sun", "below_horizon")
    await hass.async_block_till_done()
    yield freezer
    if hass.services.has_service(automation.DOMAIN, "turn_off"):
        await hass.services.async_call(automation.DOMAIN, "turn_off", {"entity_id": "all"}, blocking=True)
        await hass.async_block_till_done()


async def advance_to(hass: HomeAssistant, freezer, target) -> None:
    """Move the clock in one-minute steps (templates with now() render once a minute)."""
    while dt_util.utcnow() < target:
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def set_state(hass: HomeAssistant, entity_id: str, state: str, device_class: str) -> None:
    hass.states.async_set(entity_id, state, {"device_class": device_class})
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


HEATING = {
    "occupied": OCCUPIED,
    "next_booking": NEXT,
    "climate": {"entity_id": "climate.room_1"},
    "lead_minutes": 30,
    "comfort_temperature": 21,
    "eco_temperature": 16,
}


async def test_heating_before_and_during_a_booking_eco_after_it(hass: HomeAssistant, room) -> None:
    calls = async_mock_service(hass, "climate", "set_temperature")
    await use_blueprint(hass, "occupancy_heating.yaml", HEATING)
    await advance_to(hass, room, START - timedelta(minutes=31))
    assert not calls
    await advance_to(hass, room, START - timedelta(minutes=29))
    assert [c.data["temperature"] for c in calls] == [21]

    # the booking runs: the next booking sensor moves on, the room stays warm
    await advance_to(hass, room, START)
    await set_state(hass, OCCUPIED, "on", "occupancy")
    await set_state(hass, NEXT, "unknown", "timestamp")
    await advance_to(hass, room, START + timedelta(minutes=59))
    assert [c.data["temperature"] for c in calls] == [21]

    await set_state(hass, OCCUPIED, "off", "occupancy")
    await advance_to(hass, room, START + timedelta(minutes=61))
    assert [c.data["temperature"] for c in calls] == [21, 16]


async def test_heating_a_booking_made_at_short_notice_heats_at_once(hass: HomeAssistant, room) -> None:
    calls = async_mock_service(hass, "climate", "set_temperature")
    await set_state(hass, NEXT, "unknown", "timestamp")
    await use_blueprint(hass, "occupancy_heating.yaml", HEATING)
    await advance_to(hass, room, START - timedelta(minutes=10))
    assert not calls
    # booked in the app ten minutes before the start
    await set_state(hass, NEXT, START.isoformat(), "timestamp")
    assert [c.data["temperature"] for c in calls] == [21]


async def test_heating_a_cancelled_booking_sets_the_room_back(hass: HomeAssistant, room) -> None:
    calls = async_mock_service(hass, "climate", "set_temperature")
    await use_blueprint(hass, "occupancy_heating.yaml", HEATING)
    await advance_to(hass, room, START - timedelta(minutes=20))
    assert [c.data["temperature"] for c in calls] == [21]
    await set_state(hass, NEXT, "unknown", "timestamp")
    assert [c.data["temperature"] for c in calls] == [21, 16]


async def test_heating_waits_for_the_setback_delay_and_keeps_warm_for_a_following_booking(hass: HomeAssistant, room) -> None:
    calls = async_mock_service(hass, "climate", "set_temperature")
    await use_blueprint(hass, "occupancy_heating.yaml", {**HEATING, "setback_minutes": 15})
    # empty for 15 minutes before the lead time: eco, then comfort for the booking
    await advance_to(hass, room, START)
    assert [c.data["temperature"] for c in calls] == [16, 21]
    calls.clear()
    await set_state(hass, OCCUPIED, "on", "occupancy")
    # the next booking starts 20 minutes after this one ends: within the lead time
    await set_state(hass, NEXT, (START + timedelta(minutes=80)).isoformat(), "timestamp")
    await advance_to(hass, room, START + timedelta(minutes=60))
    await set_state(hass, OCCUPIED, "off", "occupancy")
    await advance_to(hass, room, START + timedelta(minutes=79))
    assert not calls, "the following booking keeps the room warm"

    # no booking follows: eco only after the setback delay
    await advance_to(hass, room, START + timedelta(minutes=80))
    await set_state(hass, OCCUPIED, "on", "occupancy")
    await set_state(hass, NEXT, "unknown", "timestamp")
    await advance_to(hass, room, START + timedelta(minutes=140))
    await set_state(hass, OCCUPIED, "off", "occupancy")
    await advance_to(hass, room, START + timedelta(minutes=154))
    assert not calls
    await advance_to(hass, room, START + timedelta(minutes=156))
    assert [c.data["temperature"] for c in calls] == [16]


LIGHT = {
    "occupied": OCCUPIED,
    "next_booking": NEXT,
    "lights": {"entity_id": "light.room_1"},
    "before_minutes": 0,
    "off_minutes": 10,
}


async def test_light_on_while_occupied_off_after_the_delay(hass: HomeAssistant, room) -> None:
    on = async_mock_service(hass, "light", "turn_on")
    off = async_mock_service(hass, "light", "turn_off")
    await use_blueprint(hass, "occupancy_light.yaml", LIGHT)
    await advance_to(hass, room, START - timedelta(minutes=1))
    assert not on
    # the room was empty for the switch-off delay: off (it is already)
    assert len(off) == 1
    off.clear()
    await advance_to(hass, room, START)
    assert len(on) == 1

    await set_state(hass, OCCUPIED, "on", "occupancy")
    await set_state(hass, NEXT, "unknown", "timestamp")
    await advance_to(hass, room, START + timedelta(minutes=60))
    await set_state(hass, OCCUPIED, "off", "occupancy")
    await advance_to(hass, room, START + timedelta(minutes=69))
    assert not off
    await advance_to(hass, room, START + timedelta(minutes=71))
    assert len(off) == 1 and len(on) == 1


async def test_light_only_when_dark(hass: HomeAssistant, room) -> None:
    on = async_mock_service(hass, "light", "turn_on")
    await set_state(hass, "sun.sun", "above_horizon", "")
    await use_blueprint(hass, "occupancy_light.yaml", {**LIGHT, "only_when_dark": True})
    await advance_to(hass, room, START)
    await set_state(hass, OCCUPIED, "on", "occupancy")
    assert not on
    # sunset during the booking
    await set_state(hass, "sun.sun", "below_horizon", "")
    assert len(on) == 1


async def test_light_a_booking_made_at_short_notice(hass: HomeAssistant, room) -> None:
    on = async_mock_service(hass, "light", "turn_on")
    await set_state(hass, NEXT, "unknown", "timestamp")
    await use_blueprint(hass, "occupancy_light.yaml", {**LIGHT, "before_minutes": 5})
    await advance_to(hass, room, START - timedelta(minutes=3))
    assert not on
    await set_state(hass, NEXT, START.isoformat(), "timestamp")
    assert len(on) == 1
