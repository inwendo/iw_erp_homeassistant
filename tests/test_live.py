"""End-to-end tests: the integration inside a real Home Assistant core against a live ERP.

    IW_E2E_ERP_HOST=http://127.0.0.1:8000 IW_E2E_ERP_TOKEN=<jwt> pytest -m live

The token needs the scopes Location, Event Booking and Event and "Allow super admin
functions": the tests create their own location, bookable, booking and smart lock
connection through the API. Skipped without the variables.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import instance_id

from custom_components.iw_erp_homeassistant.const import (
    CONF_DISPLAYS,
    CONF_EXPOSE_HA_LOCKS,
    CONF_HOST,
    CONF_IMPORT_ERP_LOCKS,
    CONF_LOCK_SECRET,
    CONF_TOKEN,
    DOMAIN,
)

HOST = os.environ.get("IW_E2E_ERP_HOST", "").rstrip("/")
TOKEN = os.environ.get("IW_E2E_ERP_TOKEN", "")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not (HOST and TOKEN), reason="IW_E2E_ERP_HOST / IW_E2E_ERP_TOKEN not set"),
]

RUN = secrets.token_hex(4)


@pytest.fixture(autouse=True)
def live_network(socket_enabled) -> None:
    """The Home Assistant test harness blocks sockets; these tests talk to the real ERP."""


async def erp(method: str, path: str, body=None, params=None):
    """Direct ERP request with the same token (test data and assertions)."""
    async with aiohttp.ClientSession() as session:
        async with session.request(
            method, f"{HOST}{path}", json=body, params=params, headers={"x-iw-jwt-token": TOKEN}
        ) as resp:
            text = await resp.text()
            assert resp.status < 400, f"{method} {path} -> {resp.status} {resp.headers.get('X-IW-ERROR-JSON')} {text[:300]}"
            return await resp.json(content_type=None) if text else {}


def local(days: int, hour: int) -> str:
    d = (datetime.now() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return d.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
async def bookable() -> dict:
    location = await erp("POST", "/api/event/base_location.json", {"data": {"name": f"HA live {RUN}"}})
    bookable = await erp(
        "POST", "/api/event/base_bookable.json", {"data": {"name": f"HA live room {RUN}", "baseLocation": location["id"]}}
    )
    booking = await erp(
        "POST",
        "/api/event/base_booking.json",
        {"data": {"baseBookable": bookable["id"], "startTime": local(1, 10), "endTime": local(1, 11)}},
    )
    return {"id": bookable["id"], "booking": booking["id"]}


def entry(options=None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=HOST,
        version=1,
        minor_version=2,
        data={CONF_HOST: HOST, CONF_TOKEN: TOKEN, CONF_LOCK_SECRET: secrets.token_hex(32)},
        options=options or {},
    )


async def test_config_flow_against_erp(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: HOST, CONF_TOKEN: "invalid"})
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: HOST, CONF_TOKEN: TOKEN})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_calendars_and_booking_webhook(hass: HomeAssistant, bookable: dict, hass_client_no_auth) -> None:
    config_entry = entry()
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("calendar", DOMAIN, f"{config_entry.entry_id}-{bookable['id']}")
    assert entity_id is not None
    start = datetime.now() + timedelta(days=1)
    events = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": entity_id,
            "start_date_time": start.replace(hour=0).strftime("%Y-%m-%d %H:%M:%S"),
            "end_date_time": start.replace(hour=23).strftime("%Y-%m-%d %H:%M:%S"),
        },
        blocking=True,
        return_response=True,
    )
    assert len(events[entity_id]["events"]) == 1

    # Home Assistant has no public https URL here, so the ERP refuses the webhook: polling fallback.
    assert config_entry.runtime_data.webhook_active is False

    # A booking notification (as the ERP would send it) patches the calendar from the live ERP.
    second = await erp(
        "POST",
        "/api/event/base_booking.json",
        {"data": {"baseBookable": bookable["id"], "startTime": local(1, 14), "endTime": local(1, 15)}},
    )
    client = await hass_client_no_auth()
    resp = await client.post(f"/api/webhook/{DOMAIN}", json={"iw_entity_id": str(second["id"]), "iw_action": "create"})
    assert resp.status == 200
    calendar = config_entry.runtime_data.coordinators[str(bookable["id"])].data
    assert len(list(calendar.walk("VEVENT"))) == 2

    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_import_erp_smart_locks(hass: HomeAssistant, bookable: dict) -> None:
    # An ERP smart lock offered by *another* Home Assistant instance (its webhook is unreachable).
    other_instance = f"other{RUN}".ljust(32, "0")
    offered = await erp(
        "POST",
        "/api/homeassistant/ha_locks",
        {
            "instance_id": other_instance,
            "name": f"Other HA {RUN}",
            "webhook_url": "https://ha-live-e2e.invalid/api/webhook/iw_erp_homeassistant",
            "secret": secrets.token_hex(32),
            "locks": [{"entity_id": f"lock.door_{RUN}", "name": f"Door {RUN}", "state": "locked", "supports_open": True}],
        },
    )
    synced = await erp("POST", f"/api/event/smart_lock_connection/{offered['connection_id']}/sync_devices.json")
    smart_lock_id = synced["devices"][0]["id"]

    config_entry = entry(options={CONF_IMPORT_ERP_LOCKS: True})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    entity_id = er.async_get(hass).async_get_entity_id("lock", DOMAIN, f"{config_entry.entry_id}-smart-lock-{smart_lock_id}")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state.state == "locked"
    assert state.attributes["provider"] == "homeassistant"

    # The ERP forwards the command to the other instance, which is unreachable: reported as an error.
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("lock", "open", {"entity_id": entity_id}, blocking=True)

    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_offer_ha_locks_to_erp(hass: HomeAssistant, bookable: dict) -> None:
    await async_process_ha_core_config(hass, {"external_url": f"https://ha-{RUN}.example.com"})
    hass.states.async_set("lock.front_door", "locked", {"friendly_name": f"Front door {RUN}", "supported_features": 1})

    config_entry = entry(options={CONF_EXPOSE_HA_LOCKS: ["lock.front_door"]})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    ha_instance = await instance_id.async_get(hass)
    connections = await erp("GET", "/api/event/smart_lock_connection.json", params={"provider": "homeassistant"})
    connection = next(c for c in connections if c.get("ha_instance_id") == ha_instance)
    assert connection["ha_webhook_url"] == f"https://ha-{RUN}.example.com/api/webhook/{DOMAIN}"
    assert connection["deleted"] is False
    details = await erp("GET", f"/api/event/smart_lock_connection/{connection['id']}.json")
    assert details["ha_devices"] == [
        {"entity_id": "lock.front_door", "name": f"Front door {RUN}", "state": "locked", "supports_open": True}
    ]

    # Switching the offer off withdraws it in the ERP.
    hass.config_entries.async_update_entry(config_entry, options={CONF_EXPOSE_HA_LOCKS: []})
    await hass.async_block_till_done()
    details = await erp("GET", f"/api/event/smart_lock_connection/{connection['id']}.json")
    assert details["deleted"] is True
    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_room_display_on_an_opendisplay_panel(hass: HomeAssistant, bookable: dict, tmp_path) -> None:
    """The ERP draws, Home Assistant uploads (a fake OpenDisplay service here) and reports back."""
    display = await erp(
        "POST",
        "/api/event/external_display.json",
        {"data": {"name": f"HA live door {RUN}", "deviceType": "opendisplay", "baseBookable": bookable["id"],
                  "providerConfig": {"width": 400, "height": 300}}},
    )
    hass.config.media_dirs = {"local": str(tmp_path)}

    od_entry = MockConfigEntry(domain="opendisplay", title="OpenDisplay live")
    od_entry.add_to_hass(hass)
    uploads: list[ServiceCall] = []

    async def upload(call: ServiceCall) -> None:
        uploads.append(call)

    hass.services.async_register("opendisplay", "upload_image", upload)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=od_entry.entry_id, identifiers={("opendisplay", RUN)}, sw_version="2.4.1"
    )
    sensor = er.async_get(hass).async_get_or_create(
        "sensor", "opendisplay", f"{RUN}_battery_voltage", config_entry=od_entry, device_id=device.id,
        translation_key="battery_voltage",
    )
    hass.states.async_set(sensor.entity_id, "2310", {"unit_of_measurement": "mV"})

    # The options flow offers the display and stores the assignment.
    config_entry = entry()
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IMPORT_ERP_LOCKS: False, CONF_EXPOSE_HA_LOCKS: []}
    )
    assert result["step_id"] == "displays"
    label = next(str(key) for key in result["data_schema"].schema if str(key).endswith(f"(#{display['id']})"))
    result = await hass.config_entries.options.async_configure(result["flow_id"], {label: device.id})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_DISPLAYS] == {str(display["id"]): device.id}
    await hass.async_block_till_done(wait_background_tasks=True)

    # A real 400x300 PNG from the ERP went to the panel.
    assert len(uploads) == 1
    with open(tmp_path / "iw_erp_displays" / f"{display['id']}.png", "rb") as handle:
        png = handle.read()
    assert png[1:4] == b"PNG"
    assert int.from_bytes(png[16:20], "big") == 400 and int.from_bytes(png[20:24], "big") == 300

    # Battery and last contact arrived in the ERP.
    stored = await erp("GET", f"/api/event/external_display/{display['id']}.json")
    assert abs(stored["battery_voltage"] - 2.31) < 0.001
    assert stored["firmware_version"] == "2.4.1"
    assert stored["last_seen_at"]

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await erp("PATCH", f"/api/event/external_display/{display['id']}.json", {"data": {"deleted": True}})
