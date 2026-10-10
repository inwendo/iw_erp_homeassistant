"""The opt-in functions of 1.4 with a mocked ERP: occupancy, access / check-in events,
services, usage counters, display images, repairs and diagnostics."""
from __future__ import annotations

import logging
from datetime import timedelta
from http import HTTPStatus

import pytest
import voluptuous as vol
from pytest_homeassistant_custom_component.common import async_capture_events, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.components import automation
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import instance_id
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.iw_erp_homeassistant.const import (
    CONF_ACCESS_EVENTS,
    CONF_DISPLAY_IMAGES,
    CONF_EXPOSE_HA_LOCKS,
    CONF_IMPORT_ERP_LOCKS,
    CONF_OCCUPANCY,
    CONF_USAGE_COUNTER_MAP,
    CONF_USAGE_COUNTERS,
    DOMAIN,
    EVENT_ACCESS,
    EVENT_CHECK_IN,
)
from custom_components.iw_erp_homeassistant.diagnostics import async_get_config_entry_diagnostics
from custom_components.iw_erp_homeassistant.events import webhook_token
from custom_components.iw_erp_homeassistant.logbook import describe_access, describe_check_in

from .const import HOST, SECRET, TOKEN
from .test_integration import WEBHOOK_PATH, calls_to, make_entry, mock_erp

OCCUPANCY = f"{HOST}/api/homeassistant/occupancy"
COUNTERS = f"{HOST}/api/homeassistant/usage_counters"


def disabled(setting: str) -> dict:
    """How the ERP answers a function switched off in its settings."""
    return {
        "status": 400,
        "headers": {"X-IW-ERROR-CODE": "404", "X-IW-ERROR-JSON": f'["feature disabled: {setting}"]'},
    }


def occupancy_answer(occupied: bool = True) -> dict:
    current = {"id": 123, "title": "Training U12", "start": "2030-01-01T10:00:00+01:00",
               "end": "2030-01-01T11:30:00+01:00", "participants": 14}
    nxt = {"id": 124, "title": "Yoga", "start": "2030-01-01T12:00:00+01:00",
           "end": "2030-01-01T13:00:00+01:00", "participants": 8}
    return {
        "generated_at": "2030-01-01T10:30:00+01:00",
        "bookables": [
            {
                "id": 1, "name": "Room 1", "location_id": 2, "location_name": "Sportzentrum",
                "occupied": occupied, "pre_start_active": False, "post_end_active": False,
                "pre_start_minutes": 30, "post_end_minutes": 15,
                "current_booking": current if occupied else None,
                "next_booking": nxt,
                "opening_hours": {"open_now": True, "next_open": None, "next_close": "2030-01-01T22:00:00+01:00"},
            }
        ],
    }


async def setup_entry(hass: HomeAssistant, options: dict):
    entry = make_entry(options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# --- occupancy ---------------------------------------------------------------


async def test_occupancy_sensors(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_client_no_auth) -> None:
    aioclient_mock.get(OCCUPANCY, json=occupancy_answer())
    mock_erp(aioclient_mock, bookables=[{"id": 1, "name": "Room 1"}])
    aioclient_mock.get(f"{HOST}/api/homeassistant/booking/42", status=404)
    entry = await setup_entry(hass, {CONF_OCCUPANCY: True})

    assert hass.states.get("binary_sensor.room_1_occupied").state == "on"
    assert hass.states.get("binary_sensor.room_1_occupied").attributes["title"] == "Training U12"
    assert hass.states.get("binary_sensor.room_1_pre_start").state == "off"
    assert hass.states.get("binary_sensor.room_1_open").state == "on"
    assert hass.states.get("sensor.room_1_participants").state == "14"
    next_booking = hass.states.get("sensor.room_1_next_booking")
    assert dt_util.parse_datetime(next_booking.state) == dt_util.parse_datetime("2030-01-01T11:00:00+00:00")
    assert next_booking.attributes["title"] == "Yoga"
    assert next_booking.attributes["participants"] == 8

    # The calendar joins the bookable's device.
    registry = er.async_get(hass)
    calendar_device = registry.async_get("calendar.room_1").device_id
    assert calendar_device is not None
    assert registry.async_get("binary_sensor.room_1_occupied").device_id == calendar_device
    assert dr.async_get(hass).async_get(calendar_device).name == "Room 1"

    # A booking webhook refreshes the occupancy at once.
    before = len(calls_to(aioclient_mock, "GET", "/api/homeassistant/occupancy"))
    client = await hass_client_no_auth()
    resp = await client.post(WEBHOOK_PATH, json={"iw_entity_id": "42", "iw_action": "delete"})
    assert resp.status == 200
    await hass.async_block_till_done()
    assert len(calls_to(aioclient_mock, "GET", "/api/homeassistant/occupancy")) > before

    # Polled every minute.
    before = len(calls_to(aioclient_mock, "GET", "/api/homeassistant/occupancy"))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert len(calls_to(aioclient_mock, "GET", "/api/homeassistant/occupancy")) > before
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_no_occupancy_requests_without_option(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    mock_erp(aioclient_mock, bookables=[{"id": 1, "name": "Room 1"}])
    await setup_entry(hass, {})
    assert not calls_to(aioclient_mock, "GET", "/api/homeassistant/occupancy")
    assert not calls_to(aioclient_mock, "GET", "/api/homeassistant/usage_counters")
    assert hass.states.get("binary_sensor.room_1_occupied") is None
    # the webhook asks for no events
    assert "events" not in calls_to(aioclient_mock, "POST", "/api/homeassistant/webhook")[0][2]


async def test_occupancy_feature_disabled_is_quiet(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, caplog: pytest.LogCaptureFixture
) -> None:
    aioclient_mock.get(OCCUPANCY, **disabled("integration_occupancy_api_enabled"))
    mock_erp(aioclient_mock, bookables=[{"id": 1, "name": "Room 1"}])
    with caplog.at_level(logging.INFO):
        entry = await setup_entry(hass, {CONF_OCCUPANCY: True})
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=31))
        await hass.async_block_till_done()

    assert entry.state.recoverable or entry.state.name == "LOADED"
    assert hass.states.get("binary_sensor.room_1_occupied") is None
    assert not hass.config_entries.flow.async_progress()  # no reauth
    disabled_lines = [r for r in caplog.records if "occupancy is not available" in r.getMessage()]
    assert len(disabled_lines) == 1
    assert disabled_lines[0].levelno == logging.INFO
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "occupancy" in r.getMessage()]
    assert len(calls_to(aioclient_mock, "GET", "/api/homeassistant/occupancy")) == 2


async def test_occupancy_missing_scope_raises_repair_issue(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(OCCUPANCY, status=400, headers={"X-IW-ERROR-CODE": "403", "X-IW-ERROR-JSON": '["scope"]'})
    mock_erp(aioclient_mock)
    entry = await setup_entry(hass, {CONF_OCCUPANCY: True})
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"missing_scope_{entry.entry_id}_occupancy")
    assert issue is not None
    assert issue.translation_placeholders["scope"] == "Location (read)"
    assert not hass.config_entries.flow.async_progress()

    # fixed in the ERP: the issue goes away
    aioclient_mock.clear_requests()
    aioclient_mock.get(OCCUPANCY, json=occupancy_answer())
    await entry.runtime_data.occupancy.async_refresh()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"missing_scope_{entry.entry_id}_occupancy") is None


# --- access and check-in events ----------------------------------------------

ACCESS = {
    "iw_action": "create",
    "iw_entity_class": "Inwendo\\EventBundle\\Entity\\SmartLockLog",
    "iw_entity_short_class": "smart_lock_log",
    "iw_entity_id": 991,
    "webhook_data": {
        "type": "access", "smart_lock_id": 5, "smart_lock_name": "Front door", "action": "unlock",
        "success": True, "person_id": 55, "person_name": "Max Mustermann", "source": "app",
        "at": "2030-01-01T10:00:00+01:00",
    },
}
CHECK_IN = {
    "iw_action": "create",
    "iw_entity_class": "Inwendo\\EventBundle\\Entity\\AttendanceListEntry",
    "iw_entity_short_class": "attendancelistentry",
    "iw_entity_id": 77,
    "webhook_data": {
        "type": "check_in", "attendance_list_id": 9, "attendance_list_name": "Fitnessstudio",
        "person_id": 55, "person_name": "Max Mustermann", "checked_in": True, "at": "2030-01-01T10:00:00+01:00",
    },
}


async def test_access_and_check_in_events(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_client_no_auth
) -> None:
    aioclient_mock.post(
        f"{HOST}/api/homeassistant/webhook",
        json={"status": "registered", "webhook_id": 7, "events": ["access", "check_in"]},
    )
    mock_erp(aioclient_mock, bookables=[{"id": 1, "name": "Room 1"}], smart_locks=[{"id": 5, "name": "Front door"}])
    entry = await setup_entry(hass, {CONF_ACCESS_EVENTS: True, CONF_IMPORT_ERP_LOCKS: True})

    body = calls_to(aioclient_mock, "POST", "/api/homeassistant/webhook")[0][2]
    assert body["events"] == ["access", "check_in"]
    assert body["webhook_token"] == webhook_token(entry)
    assert entry.runtime_data.webhook_events == ["access", "check_in"]

    access_events = async_capture_events(hass, EVENT_ACCESS)
    check_in_events = async_capture_events(hass, EVENT_CHECK_IN)
    client = await hass_client_no_auth()
    token = {"X-IW-Webhook-Token": webhook_token(entry)}

    resp = await client.post(WEBHOOK_PATH, json=ACCESS, headers=token)
    assert resp.status == 200
    await hass.async_block_till_done()
    assert len(access_events) == 1
    data = access_events[0].data
    assert data["smart_lock_id"] == 5
    assert data["person_name"] == "Max Mustermann"
    assert data["success"] is True
    assert data["config_entry_id"] == entry.entry_id
    assert data["verified"] is True
    # never treated as a booking id
    assert not calls_to(aioclient_mock, "GET", "/api/homeassistant/booking/991")

    # last access sensors: per config entry and per imported lock
    last = hass.states.get("sensor.inwendo_erp_erp_example_com_last_access")
    assert last is not None
    assert last.attributes["person"] == "Max Mustermann"
    assert last.attributes["lock"] == "Front door"
    assert last.attributes["success"] is True
    assert dt_util.parse_datetime(last.state) == dt_util.parse_datetime("2030-01-01T09:00:00+00:00")
    assert hass.states.get("sensor.front_door_last_access").attributes["action"] == "unlock"

    # "status" log entries are no access
    resp = await client.post(WEBHOOK_PATH, json={**ACCESS, "webhook_data": {**ACCESS["webhook_data"], "action": "status"}})
    assert resp.status == 200
    assert len(access_events) == 1

    # a wrong token is rejected
    resp = await client.post(WEBHOOK_PATH, json=ACCESS, headers={"X-IW-Webhook-Token": "nope"})
    assert resp.status == HTTPStatus.UNAUTHORIZED
    assert len(access_events) == 1

    # without a token (ERP not sending it yet): fired, unverified
    resp = await client.post(WEBHOOK_PATH, json=ACCESS)
    assert len(access_events) == 2
    assert access_events[1].data["verified"] is False

    # check-in and check-out; deletes without summary are ignored
    await client.post(WEBHOOK_PATH, json=CHECK_IN, headers=token)
    checkout = {**CHECK_IN, "iw_action": "update", "webhook_data": {**CHECK_IN["webhook_data"], "checked_in": False}}
    await client.post(WEBHOOK_PATH, json=checkout, headers=token)
    await client.post(
        WEBHOOK_PATH,
        json={k: v for k, v in CHECK_IN.items() if k != "webhook_data"} | {"iw_action": "delete"},
        headers=token,
    )
    assert [e.data["checked_in"] for e in check_in_events] == [True, False]
    assert check_in_events[0].data["attendance_list_name"] == "Fitnessstudio"
    assert not calls_to(aioclient_mock, "GET", "/api/homeassistant/booking/77")


async def test_booking_webhook_with_erp_class_names(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_client_no_auth
) -> None:
    """The ERP sends BaseBooking as short class "booking"; other classes are ignored."""
    mock_erp(aioclient_mock, bookables=[{"id": 1, "name": "Room 1"}])
    aioclient_mock.get(f"{HOST}/api/homeassistant/booking/42", status=404)
    await setup_entry(hass, {})
    client = await hass_client_no_auth()
    resp = await client.post(
        WEBHOOK_PATH,
        json={"iw_entity_id": 42, "iw_action": "update", "iw_entity_short_class": "booking",
              "iw_entity_class": "Inwendo\\EventBundle\\Entity\\BaseBooking"},
    )
    assert resp.status == 200
    assert len(calls_to(aioclient_mock, "GET", "/api/homeassistant/booking/42")) == 1

    # access events without the option: no event, no booking request
    events = async_capture_events(hass, EVENT_ACCESS)
    resp = await client.post(WEBHOOK_PATH, json=ACCESS)
    assert resp.status == 200
    assert not events
    resp = await client.post(WEBHOOK_PATH, json={"iw_entity_id": 3, "iw_entity_class": "Inwendo\\X\\Entity\\Other"})
    assert resp.status == 200
    assert not calls_to(aioclient_mock, "GET", "/api/homeassistant/booking/991")
    assert not calls_to(aioclient_mock, "GET", "/api/homeassistant/booking/3")


async def test_access_events_disabled_in_erp(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, caplog: pytest.LogCaptureFixture
) -> None:
    aioclient_mock.post(f"{HOST}/api/homeassistant/webhook", json={"status": "registered", "webhook_id": 7, "events": []})
    mock_erp(aioclient_mock)
    with caplog.at_level(logging.INFO):
        entry = await setup_entry(hass, {CONF_ACCESS_EVENTS: True})
    assert entry.runtime_data.webhook_events == []
    assert entry.runtime_data.features.available["access_events"] is False
    assert "integration_ha_access_events_enabled" in caplog.text


async def test_device_triggers_and_logbook(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_client_no_auth
) -> None:
    mock_erp(aioclient_mock)
    entry = await setup_entry(hass, {CONF_ACCESS_EVENTS: True})
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert device is not None

    from homeassistant.components.device_automation import DeviceAutomationType, async_get_device_automations

    triggers = await async_get_device_automations(hass, DeviceAutomationType.TRIGGER, [device.id])
    assert {t["type"] for t in triggers[device.id]} == {"access_granted", "access_denied", "checked_in", "checked_out"}

    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: [
                {
                    "triggers": {"platform": "device", "domain": DOMAIN, "device_id": device.id, "type": "access_denied"},
                    "actions": {"event": "denied_seen"},
                }
            ]
        },
    )
    seen = async_capture_events(hass, "denied_seen")
    client = await hass_client_no_auth()
    await client.post(WEBHOOK_PATH, json=ACCESS)
    denied = {**ACCESS, "webhook_data": {**ACCESS["webhook_data"], "action": "access_denied", "success": False}}
    await client.post(WEBHOOK_PATH, json=denied)
    await hass.async_block_till_done()
    assert len(seen) == 1

    assert describe_access(ACCESS["webhook_data"]) == {"name": "Max Mustermann", "message": "opened Front door via app"}
    assert describe_access(denied["webhook_data"])["message"] == "was denied access to Front door via app"
    assert describe_check_in(CHECK_IN["webhook_data"])["message"] == "checked in at Fitnessstudio"


# --- services ----------------------------------------------------------------

CHECK = f"{HOST}/api/homeassistant/check_access"
TICKET = f"{HOST}/api/homeassistant/ticket"
GRANTED = {"granted": True, "reason": "granted", "credential_type": "card", "smart_lock": {"id": 5, "name": "Front door"},
           "person": {"id": 55, "name": "Max Mustermann"}, "opened": None}


async def test_check_access_service(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(CHECK, json=GRANTED)
    mock_erp(aioclient_mock, smart_locks=[{"id": 5, "name": "Front door"}])
    await setup_entry(hass, {CONF_IMPORT_ERP_LOCKS: True})

    result = await hass.services.async_call(
        DOMAIN, "check_access", {"credential": "04A1B2C3D4", "smart_lock_id": 5, "open": True},
        blocking=True, return_response=True,
    )
    assert result == GRANTED
    body = calls_to(aioclient_mock, "POST", "/api/homeassistant/check_access")[-1][2]
    assert body == {"credential": "04A1B2C3D4", "credential_type": "auto", "smart_lock_id": 5, "open": True}

    # an imported ERP lock entity -> its smart lock id
    await hass.services.async_call(
        DOMAIN, "check_access", {"credential": "1234", "credential_type": "access_code", "lock": "lock.front_door"},
        blocking=True, return_response=True,
    )
    assert calls_to(aioclient_mock, "POST", "/api/homeassistant/check_access")[-1][2]["smart_lock_id"] == 5

    # another Home Assistant lock -> instance id + entity id
    await hass.services.async_call(
        DOMAIN, "check_access", {"credential": "1234", "lock": "lock.garage"}, blocking=True, return_response=True
    )
    body = calls_to(aioclient_mock, "POST", "/api/homeassistant/check_access")[-1][2]
    assert body["lock_id"] == "lock.garage"
    assert body["instance_id"] == await instance_id.async_get(hass)

    # a lock is required
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "check_access", {"credential": "x"}, blocking=True, return_response=True)


async def test_check_access_feature_disabled(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(CHECK, **disabled("integration_ha_access_check_enabled"))
    mock_erp(aioclient_mock)
    await setup_entry(hass, {})
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN, "check_access", {"credential": "x", "smart_lock_id": 5}, blocking=True, return_response=True
        )
    assert err.value.translation_key == "feature_disabled"
    assert not hass.config_entries.flow.async_progress()


async def test_check_access_unknown_lock_is_no_feature_error(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(CHECK, status=400, headers={"X-IW-ERROR-CODE": "404", "X-IW-ERROR-JSON": '["smart lock not found"]'})
    mock_erp(aioclient_mock)
    await setup_entry(hass, {})
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN, "check_access", {"credential": "x", "smart_lock_id": 99}, blocking=True, return_response=True
        )
    assert err.value.translation_key == "request_failed"
    assert "smart lock not found" in err.value.translation_placeholders["error"]


async def test_create_ticket_service(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(TICKET, json={"id": 88, "status": "created"})
    mock_erp(aioclient_mock, bookables=[{"id": 1, "name": "Room 1"}])
    await setup_entry(hass, {})
    device_id = er.async_get(hass).async_get("calendar.room_1").device_id

    result = await hass.services.async_call(
        DOMAIN,
        "create_ticket",
        {"title": "Wasseralarm", "description": "Sensor nass", "bookable": device_id, "priority": "urgent",
         "dedupe_key": "water_room1"},
        blocking=True,
        return_response=True,
    )
    assert result == {"id": 88, "status": "created"}
    body = calls_to(aioclient_mock, "POST", "/api/homeassistant/ticket")[0][2]
    assert body == {"title": "Wasseralarm", "description": "Sensor nass", "bookable_id": 1, "priority": "urgent",
                    "dedupe_key": "water_room1"}
    assert calls_to(aioclient_mock, "POST", "/api/homeassistant/ticket")[0][3]["x-iw-jwt-token"] == TOKEN


async def test_create_ticket_missing_scope(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(TICKET, status=403)
    mock_erp(aioclient_mock)
    await setup_entry(hass, {})
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(DOMAIN, "create_ticket", {"title": "x"}, blocking=True, return_response=True)
    assert err.value.translation_key == "missing_scope"
    assert err.value.translation_placeholders["scope"] == "Ticket (write)"
    assert not hass.config_entries.flow.async_progress()


async def test_service_needs_a_loaded_entry(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    mock_erp(aioclient_mock)
    entry = await setup_entry(hass, {})
    assert await hass.config_entries.async_unload(entry.entry_id)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "create_ticket", {"title": "x"}, blocking=True, return_response=True)


# --- usage counters ----------------------------------------------------------

COUNTER_LIST = [
    {"id": 3, "name": "Stromzähler Halle", "bookable_id": 1, "bookable_name": "Room 1", "value": 1234.5,
     "unit": "kWh", "use_real_usage_data": False, "updated": "2030-01-01T10:00:00+01:00"},
    {"id": 4, "name": "Wasser", "bookable_id": None, "bookable_name": None, "value": 10,
     "unit": "m³", "use_real_usage_data": True, "updated": None},
]


async def test_usage_counters(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer) -> None:
    hass.states.async_set("sensor.hall_meter", "1240.2", {"unit_of_measurement": "kWh"})
    aioclient_mock.get(COUNTERS, json=COUNTER_LIST)
    aioclient_mock.post(f"{COUNTERS}/3", json={"id": 3, "value": 1240.2, "updated": "2030-01-01T10:05:00+01:00"})
    mock_erp(aioclient_mock, bookables=[{"id": 1, "name": "Room 1"}])
    entry = await setup_entry(hass, {CONF_USAGE_COUNTERS: True, CONF_USAGE_COUNTER_MAP: {"3": "sensor.hall_meter"}})
    await hass.async_block_till_done(wait_background_tasks=True)

    reports = calls_to(aioclient_mock, "POST", "/api/homeassistant/usage_counters/3")
    assert [r[2] for r in reports] == [{"value": 1240.2}]
    state = hass.states.get("sensor.stromzahler_halle")
    assert state.state == "1240.2"
    assert state.attributes["unit_of_measurement"] == "kWh"
    assert state.attributes["device_class"] == "energy"
    assert hass.states.get("sensor.wasser").state == "10"
    # the counter sits on the bookable's device
    registry = er.async_get(hass)
    assert registry.async_get("sensor.stromzahler_halle").device_id == registry.async_get("calendar.room_1").device_id

    # throttled: a second reading within 15 minutes is sent when the interval is over
    hass.states.async_set("sensor.hall_meter", "1241.0")
    hass.states.async_set("sensor.hall_meter", "unavailable")
    hass.states.async_set("sensor.hall_meter", "1242.5")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(calls_to(aioclient_mock, "POST", "/api/homeassistant/usage_counters/3")) == 1
    freezer.tick(timedelta(minutes=16))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    reports = calls_to(aioclient_mock, "POST", "/api/homeassistant/usage_counters/3")
    assert [r[2] for r in reports] == [{"value": 1240.2}, {"value": 1242.5}]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_usage_counters_feature_disabled(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    hass.states.async_set("sensor.hall_meter", "1240.2")
    aioclient_mock.get(COUNTERS, **disabled("integration_usage_counter_api_enabled"))
    mock_erp(aioclient_mock)
    entry = await setup_entry(hass, {CONF_USAGE_COUNTERS: True, CONF_USAGE_COUNTER_MAP: {"3": "sensor.hall_meter"}})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not calls_to(aioclient_mock, "POST", "/api/homeassistant/usage_counters/3")
    assert not [s for s in hass.states.async_entity_ids("sensor") if s != "sensor.hall_meter"]
    assert not hass.config_entries.flow.async_progress()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_options_flow_maps_usage_counters(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    hass.states.async_set("sensor.hall_meter", "1")
    aioclient_mock.get(COUNTERS, json=COUNTER_LIST)
    aioclient_mock.post(f"{COUNTERS}/3", json={"id": 3, "value": 1})
    mock_erp(aioclient_mock)
    entry = await setup_entry(hass, {})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_IMPORT_ERP_LOCKS: False, CONF_EXPOSE_HA_LOCKS: [], CONF_OCCUPANCY: False, CONF_ACCESS_EVENTS: False,
         CONF_USAGE_COUNTERS: True, CONF_DISPLAY_IMAGES: False},
    )
    assert result["step_id"] == "usage_counters"
    # counters fed by real usage data are not offered
    assert [str(key) for key in result["data_schema"].schema] == ["Stromzähler Halle – Room 1 [kWh] (#3)"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"Stromzähler Halle – Room 1 [kWh] (#3)": "sensor.hall_meter"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.options == {
        CONF_IMPORT_ERP_LOCKS: False, CONF_EXPOSE_HA_LOCKS: [], CONF_USAGE_COUNTERS: True,
        CONF_USAGE_COUNTER_MAP: {"3": "sensor.hall_meter"},
    }


# --- room display images -----------------------------------------------------


async def test_display_images(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_client) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 40
    aioclient_mock.get(f"{HOST}/api/homeassistant/displays", json=[{"id": 13, "name": "Foyer"}])
    aioclient_mock.get(
        f"{HOST}/api/homeassistant/displays/13/frame.png",
        content=png,
        headers={"ETag": '"e1"', "X-Refresh-After": "600", "Content-Type": "image/png"},
    )
    mock_erp(aioclient_mock)
    entry = await setup_entry(hass, {CONF_DISPLAY_IMAGES: True})
    await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get("image.foyer")
    assert state is not None
    assert state.state != "unknown"
    client = await hass_client()
    resp = await client.get(state.attributes["entity_picture"])
    assert resp.status == 200
    assert await resp.read() == png
    # no OpenDisplay panel: no upload, no telemetry
    assert not calls_to(aioclient_mock, "POST", "/api/homeassistant/displays/13/telemetry")

    # the next round sends the ETag
    aioclient_mock.clear_requests()
    mock_erp(aioclient_mock)
    aioclient_mock.get(f"{HOST}/api/homeassistant/displays/13/frame.png", status=304, headers={"X-Refresh-After": "600"})
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=601))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls_to(aioclient_mock, "GET", "/api/homeassistant/displays/13/frame.png")[0][3]["If-None-Match"] == '"e1"'
    assert await hass.config_entries.async_unload(entry.entry_id)


# --- repairs and diagnostics -------------------------------------------------


async def test_no_external_url_issue(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(
        f"{HOST}/api/homeassistant/webhook",
        status=400,
        headers={"X-IW-ERROR-CODE": "400", "X-IW-ERROR-JSON": '["webhook_url must be https"]'},
    )
    mock_erp(aioclient_mock)
    entry = await setup_entry(hass, {})
    assert not entry.runtime_data.webhook_active
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"no_external_url_{entry.entry_id}") is not None


async def test_issue_cleared_when_webhook_registers(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, external_url
) -> None:
    mock_erp(aioclient_mock)
    entry = make_entry()
    entry.add_to_hass(hass)
    ir.async_create_issue(
        hass, DOMAIN, f"no_external_url_{entry.entry_id}", is_fixable=False,
        severity=ir.IssueSeverity.WARNING, translation_key="no_external_url",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.webhook_active
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"no_external_url_{entry.entry_id}") is None


async def test_api_key_rejected_issue(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{HOST}/api/homeassistant/bookables", status=400, headers={"X-IW-ERROR-CODE": "401"})
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"api_key_rejected_{entry.entry_id}") is not None
    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress()] == ["reauth"]


async def test_diagnostics_are_redacted(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(OCCUPANCY, json=occupancy_answer())
    mock_erp(aioclient_mock, bookables=[{"id": 1, "name": "Room 1"}])
    entry = await setup_entry(hass, {CONF_OCCUPANCY: True})

    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    assert TOKEN not in text
    assert SECRET not in text
    assert webhook_token(entry) not in text
    assert "Training U12" not in text  # booking titles can be personal
    assert diag["entry"]["data"]["api_key"] == "**REDACTED**"
    assert diag["runtime"]["occupancy"]["data"]["1"]["occupied"] is True
    assert diag["runtime"]["webhook_active"] is True

    # credentials typed into the host URL never reach the download
    other = make_entry(host="https://user:pw@erp2.example.com")
    diag = await async_get_config_entry_diagnostics(hass, other)
    assert diag["entry"]["data"]["host"] == "https://erp2.example.com"
    assert "pw" not in str(diag)
