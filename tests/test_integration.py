"""Integration tests with a mocked ERP (pytest-homeassistant-custom-component)."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import instance_id
from homeassistant.util import dt as dt_util

from custom_components.iw_erp_homeassistant.const import (
    CONF_EXPOSE_HA_LOCKS,
    CONF_HOST,
    CONF_IMPORT_ERP_LOCKS,
    CONF_LOCK_SECRET,
    CONF_TOKEN,
    DOMAIN,
)

from .const import HOST, ICAL, SECRET, TOKEN

WEBHOOK_PATH = f"/api/webhook/{DOMAIN}"


def mock_erp(aioclient_mock: AiohttpClientMocker, bookables=None, smart_locks=None) -> None:
    aioclient_mock.get(f"{HOST}/api/homeassistant/bookables", json=bookables or [])
    aioclient_mock.get(
        f"{HOST}/api/homeassistant/calendar/1",
        text=ICAL,
        headers={"Content-Type": "text/calendar"},
    )
    aioclient_mock.post(f"{HOST}/api/homeassistant/webhook", json={"status": "registered", "webhook_id": 7})
    aioclient_mock.delete(f"{HOST}/api/homeassistant/webhook", json={"status": "unregistered", "count": 1})
    aioclient_mock.get(f"{HOST}/api/homeassistant/smart_locks", json=smart_locks or [])
    aioclient_mock.get(f"{HOST}/api/homeassistant/smart_locks/5/status", json={"id": 5, "state": "locked"})
    aioclient_mock.post(f"{HOST}/api/homeassistant/smart_locks/5/lock", json={"success": True})
    aioclient_mock.post(f"{HOST}/api/homeassistant/smart_locks/5/unlock", json={"success": True})
    aioclient_mock.post(f"{HOST}/api/homeassistant/ha_locks", json={"status": "created", "connection_id": 3, "locks": 1})
    aioclient_mock.delete(f"{HOST}/api/homeassistant/ha_locks", json={"status": "unregistered"})


def calls_to(aioclient_mock: AiohttpClientMocker, method: str, path: str) -> list:
    return [c for c in aioclient_mock.mock_calls if c[0] == method and str(c[1]).split("?")[0] == f"{HOST}{path}"]


def make_entry(options=None, **data) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=HOST,
        title=HOST,
        version=1,
        minor_version=2,
        data={CONF_HOST: HOST, CONF_TOKEN: TOKEN, CONF_LOCK_SECRET: SECRET, **data},
        options=options or {},
    )


@pytest.fixture
async def external_url(hass: HomeAssistant) -> None:
    await async_process_ha_core_config(hass, {"external_url": "https://ha.example.com"})


async def test_config_flow_creates_entry_with_lock_secret(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{HOST}/api/homeassistant/bookables", json=[])
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM

    mock_erp(aioclient_mock)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST + "/", CONF_TOKEN: TOKEN}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == HOST
    assert len(result["data"][CONF_LOCK_SECRET]) == 64
    assert aioclient_mock.mock_calls[0][3]["x-iw-jwt-token"] == TOKEN


async def test_config_flow_invalid_auth_shows_erp_error(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(
        f"{HOST}/api/homeassistant/bookables",
        status=401,
        headers={"X-IW-ERROR-CODE": "401", "X-IW-ERROR-JSON": '["JWT expired"]'},
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: HOST, CONF_TOKEN: "bad"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert "JWT expired" in result["description_placeholders"]["last_error"]


async def test_erp_soft_auth_error_is_invalid_auth(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The ERP answers an invalid API key with HTTP 400 and X-IW-ERROR-CODE 401."""
    aioclient_mock.get(f"{HOST}/api/homeassistant/bookables", status=400, headers={"X-IW-ERROR-CODE": "401", "X-IW-ERROR-JSON": "[]"})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: HOST, CONF_TOKEN: "bad"})
    assert result["errors"] == {"base": "invalid_auth"}

    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress()] == ["user", "reauth"]


async def test_migration_adds_lock_secret(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    mock_erp(aioclient_mock)
    entry = MockConfigEntry(domain=DOMAIN, version=1, minor_version=1, data={CONF_HOST: HOST, CONF_TOKEN: TOKEN})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.minor_version == 2
    assert len(entry.data[CONF_LOCK_SECRET]) == 64


async def test_calendar_setup_and_unload(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    mock_erp(aioclient_mock, bookables=[{"id": 1, "name": "Room 1"}])
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("calendar.room_1") is not None
    events = await entry.runtime_data.coordinators["1"].hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": "calendar.room_1", "start_date_time": "2030-01-01 00:00:00", "end_date_time": "2030-01-02 00:00:00"},
        blocking=True,
        return_response=True,
    )
    assert events["calendar.room_1"]["events"][0]["summary"] == "Meeting"
    # The ERP webhook was registered, so the calendars only poll twice a day.
    assert entry.runtime_data.webhook_active
    assert calls_to(aioclient_mock, "POST", "/api/homeassistant/webhook")
    # No lock requests unless opted in.
    assert not calls_to(aioclient_mock, "GET", "/api/homeassistant/smart_locks")
    assert not calls_to(aioclient_mock, "POST", "/api/homeassistant/ha_locks")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert calls_to(aioclient_mock, "DELETE", "/api/homeassistant/webhook")


async def test_calendar_auth_failure_starts_reauth(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{HOST}/api/homeassistant/bookables", status=401)
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]

    aioclient_mock.clear_requests()
    mock_erp(aioclient_mock)
    result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"], {CONF_TOKEN: "new-token"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == "new-token"


async def test_import_erp_locks(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    mock_erp(aioclient_mock, smart_locks=[{"id": 5, "name": "Front door", "type": "nuki"}])
    entry = make_entry(options={CONF_IMPORT_ERP_LOCKS: True})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("lock.front_door")
    assert state is not None
    assert state.state == "locked"
    list_call = calls_to(aioclient_mock, "GET", "/api/homeassistant/smart_locks")[0]
    assert list_call[1].query["excludeHaInstanceId"] == await instance_id.async_get(hass)

    await hass.services.async_call("lock", "open", {"entity_id": "lock.front_door"}, blocking=True)
    assert len(calls_to(aioclient_mock, "POST", "/api/homeassistant/smart_locks/5/unlock")) == 1
    assert hass.states.get("lock.front_door").state == "open"

    await hass.services.async_call("lock", "lock", {"entity_id": "lock.front_door"}, blocking=True)
    assert len(calls_to(aioclient_mock, "POST", "/api/homeassistant/smart_locks/5/lock")) == 1

    # the real state is fetched shortly after a command
    status_calls = len(calls_to(aioclient_mock, "GET", "/api/homeassistant/smart_locks/5/status"))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()
    assert len(calls_to(aioclient_mock, "GET", "/api/homeassistant/smart_locks/5/status")) > status_calls


async def test_import_erp_lock_command_failure(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(f"{HOST}/api/homeassistant/smart_locks/5/unlock", json={"success": False})
    mock_erp(aioclient_mock, smart_locks=[{"id": 5, "name": "Front door", "type": "nuki"}])
    entry = make_entry(options={CONF_IMPORT_ERP_LOCKS: True})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    from homeassistant.exceptions import HomeAssistantError

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("lock", "unlock", {"entity_id": "lock.front_door"}, blocking=True)
    assert hass.states.get("lock.front_door").state == "locked"


async def test_options_flow(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    mock_erp(aioclient_mock, smart_locks=[])
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IMPORT_ERP_LOCKS: True, CONF_EXPOSE_HA_LOCKS: []}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {CONF_IMPORT_ERP_LOCKS: True, CONF_EXPOSE_HA_LOCKS: []}
    # reloaded with the lock platform
    assert calls_to(aioclient_mock, "GET", "/api/homeassistant/smart_locks")


def fake_lock_services(hass: HomeAssistant) -> list[ServiceCall]:
    calls: list[ServiceCall] = []

    async def handle(call: ServiceCall) -> None:
        calls.append(call)
        new_state = {"lock": "locked", "unlock": "unlocked", "open": "open"}[call.service]
        entity_id = call.data["entity_id"]
        entity_id = entity_id[0] if isinstance(entity_id, list) else entity_id
        old = hass.states.get(entity_id)
        hass.states.async_set(entity_id, new_state, old.attributes if old else {})

    for service in ("lock", "unlock", "open"):
        hass.services.async_register("lock", service, handle)
    return calls


def signed(body: dict, secret: str = SECRET) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode()
    return raw, {
        "Content-Type": "application/json",
        "X-IW-Signature": hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(),
    }


async def test_offer_ha_locks_and_execute_signed_commands(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_client_no_auth, external_url
) -> None:
    hass.states.async_set("lock.front_door", "locked", {"friendly_name": "Front door", "supported_features": 1})
    hass.states.async_set("lock.garage", "locked", {"friendly_name": "Garage", "supported_features": 0})
    hass.states.async_set("lock.cellar", "locked", {"friendly_name": "Cellar"})
    service_calls = fake_lock_services(hass)
    mock_erp(aioclient_mock)

    entry = make_entry(options={CONF_EXPOSE_HA_LOCKS: ["lock.front_door", "lock.garage"]})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    push = calls_to(aioclient_mock, "POST", "/api/homeassistant/ha_locks")
    assert len(push) == 1
    payload = push[0][2]
    ha_instance = await instance_id.async_get(hass)
    assert payload["instance_id"] == ha_instance
    assert payload["webhook_url"] == f"https://ha.example.com{WEBHOOK_PATH}"
    assert payload["secret"] == SECRET
    assert payload["locks"] == [
        {"entity_id": "lock.front_door", "name": "Front door", "state": "locked", "supports_open": True},
        {"entity_id": "lock.garage", "name": "Garage", "state": "locked", "supports_open": False},
    ]

    client = await hass_client_no_auth()

    def command(entity_id: str, cmd: str, **overrides) -> dict:
        return {
            "type": "iw_lock_command",
            "instance_id": ha_instance,
            "entity_id": entity_id,
            "command": cmd,
            "ts": int(time.time()),
            "nonce": f"{entity_id}-{cmd}-{time.monotonic_ns()}",
            **overrides,
        }

    # open on a lock with the OPEN feature -> lock.open
    body, headers = signed(command("lock.front_door", "open"))
    resp = await client.post(WEBHOOK_PATH, data=body, headers=headers)
    assert resp.status == 200
    assert await resp.json() == {"success": True, "state": "open"}
    assert service_calls[-1].service == "open"

    # replaying the same request is rejected
    resp = await client.post(WEBHOOK_PATH, data=body, headers=headers)
    assert resp.status == 400

    # open without the OPEN feature -> lock.unlock
    body, headers = signed(command("lock.garage", "open"))
    resp = await client.post(WEBHOOK_PATH, data=body, headers=headers)
    assert (await resp.json())["state"] == "unlocked"
    assert service_calls[-1].service == "unlock"

    # lock that is not offered
    body, headers = signed(command("lock.cellar", "open"))
    resp = await client.post(WEBHOOK_PATH, data=body, headers=headers)
    assert resp.status == 403

    # wrong secret
    body, headers = signed(command("lock.front_door", "lock"), secret="b" * 64)
    resp = await client.post(WEBHOOK_PATH, data=body, headers=headers)
    assert resp.status == 401

    # stale command
    body, headers = signed(command("lock.front_door", "lock", ts=int(time.time()) - 3600))
    resp = await client.post(WEBHOOK_PATH, data=body, headers=headers)
    assert resp.status == 400
    assert len(service_calls) == 2

    # state changes are pushed to the ERP (debounced)
    hass.states.async_set("lock.front_door", "unlocked", {"friendly_name": "Front door", "supported_features": 1})
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()
    push = calls_to(aioclient_mock, "POST", "/api/homeassistant/ha_locks")
    assert push[-1][2]["locks"][0]["state"] == "unlocked"

    # switching the offer off withdraws the locks in the ERP
    hass.config_entries.async_update_entry(entry, options={CONF_EXPOSE_HA_LOCKS: []})
    await hass.async_block_till_done()
    withdraw = calls_to(aioclient_mock, "DELETE", "/api/homeassistant/ha_locks")
    assert len(withdraw) == 1
    assert withdraw[0][2] == {"instance_id": ha_instance, "secret": SECRET}


async def test_booking_webhook_patches_calendar(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_client_no_auth
) -> None:
    mock_erp(aioclient_mock, bookables=[{"id": 1, "name": "Room 1"}])
    aioclient_mock.get(
        f"{HOST}/api/homeassistant/booking/42",
        text=ICAL.replace("booking-1@erp", "booking-42@erp").replace("Meeting", "Workshop"),
        headers={"X-Bookable-Id": "1"},
    )
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client = await hass_client_no_auth()
    resp = await client.post(WEBHOOK_PATH, json={"iw_entity_id": "42", "iw_action": "create"})
    assert resp.status == 200
    calendar = entry.runtime_data.coordinators["1"].data
    assert sorted(str(e.get("summary")) for e in calendar.walk("VEVENT")) == ["Meeting", "Workshop"]
