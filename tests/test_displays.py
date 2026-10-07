"""ERP room displays on OpenDisplay panels, with a mocked ERP and a fake OpenDisplay integration."""
from __future__ import annotations

import os
from datetime import timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.iw_erp_homeassistant.const import (
    CONF_DISPLAYS,
    CONF_EXPOSE_HA_LOCKS,
    CONF_IMPORT_ERP_LOCKS,
    DISPLAY_MEDIA_FOLDER,
)

from .const import HOST
from .test_integration import calls_to, make_entry, mock_erp

PNG = b"\x89PNG\r\n\x1a\n" + b"frame" * 20
FRAME = f"{HOST}/api/homeassistant/displays/12/frame.png"
TELEMETRY = f"{HOST}/api/homeassistant/displays/12/telemetry"
DISPLAYS = [
    {"id": 12, "name": "Tür Raum 1", "device_id": "DSPabc", "target": "Raum 1", "refresh_seconds": 300, "width": 800, "height": 480},
    {"id": 13, "name": "Foyer", "device_id": "DSPdef", "target": None, "refresh_seconds": 300, "width": 800, "height": 480},
]


def fake_opendisplay(hass: HomeAssistant) -> tuple[MockConfigEntry, list[ServiceCall]]:
    """The OpenDisplay integration as far as we use it: its config entry and upload service."""
    entry = MockConfigEntry(domain="opendisplay", title="OpenDisplay A1B2")
    entry.add_to_hass(hass)
    calls: list[ServiceCall] = []

    async def upload(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register("opendisplay", "upload_image", upload)
    return entry, calls


def panel(hass: HomeAssistant, od_entry: MockConfigEntry) -> str:
    """An OpenDisplay device with its battery and signal sensors."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=od_entry.entry_id,
        identifiers={("opendisplay", "A1B2")},
        name="OpenDisplay A1B2",
        sw_version="2.4.1",
    )
    registry = er.async_get(hass)
    for key, value, unit in (("battery_voltage", "2950", "mV"), ("rssi", "-71", "dBm")):
        entity = registry.async_get_or_create(
            "sensor", "opendisplay", f"A1B2_{key}", config_entry=od_entry, device_id=device.id, translation_key=key
        )
        hass.states.async_set(entity.entity_id, value, {"unit_of_measurement": unit})
    return device.id


def mock_frame(aioclient_mock: AiohttpClientMocker, status: int = 200) -> None:
    aioclient_mock.get(
        FRAME,
        status=status,
        content=PNG if status == 200 else b"",
        headers={"ETag": '"etag1"', "X-Refresh-After": "600", "Content-Type": "image/png"},
    )
    aioclient_mock.post(TELEMETRY, json={"status": "ok"})


async def test_options_flow_assigns_panels(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    mock_erp(aioclient_mock)
    aioclient_mock.get(f"{HOST}/api/homeassistant/displays", json=DISPLAYS)
    od_entry, _ = fake_opendisplay(hass)
    device_id = panel(hass, od_entry)
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IMPORT_ERP_LOCKS: False, CONF_EXPOSE_HA_LOCKS: []}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "displays"
    fields = [str(key) for key in result["data_schema"].schema]
    assert fields == ["Tür Raum 1 – Raum 1 (#12)", "Foyer (#13)"]

    mock_frame(aioclient_mock, status=304)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"Tür Raum 1 – Raum 1 (#12)": device_id}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.options[CONF_DISPLAYS] == {"12": device_id}


async def test_no_display_step_without_opendisplay(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    mock_erp(aioclient_mock)
    aioclient_mock.get(f"{HOST}/api/homeassistant/displays", json=DISPLAYS)
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IMPORT_ERP_LOCKS: False, CONF_EXPOSE_HA_LOCKS: []}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_DISPLAYS not in entry.options
    assert not calls_to(aioclient_mock, "GET", "/api/homeassistant/displays")


async def test_frame_is_uploaded_and_telemetry_reported(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tmp_path
) -> None:
    hass.config.media_dirs = {"local": str(tmp_path)}
    mock_erp(aioclient_mock)
    mock_frame(aioclient_mock)
    od_entry, uploads = fake_opendisplay(hass)
    device_id = panel(hass, od_entry)

    entry = make_entry(options={CONF_DISPLAYS: {"12": device_id}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    # Uploaded through the OpenDisplay service, from the local media folder.
    assert len(uploads) == 1
    assert uploads[0].data["device_id"] == device_id
    assert uploads[0].data["image"] == {
        "media_content_id": f"media-source://media_source/local/{DISPLAY_MEDIA_FOLDER}/12.png",
        "media_content_type": "image/png",
    }
    with open(os.path.join(tmp_path, DISPLAY_MEDIA_FOLDER, "12.png"), "rb") as handle:
        assert handle.read() == PNG

    # The API token authenticates, no device secret involved.
    frame_call = calls_to(aioclient_mock, "GET", "/api/homeassistant/displays/12/frame.png")[0]
    assert frame_call[3]["x-iw-jwt-token"] == "test-jwt"
    assert "If-None-Match" not in frame_call[3]

    telemetry = calls_to(aioclient_mock, "POST", "/api/homeassistant/displays/12/telemetry")
    assert len(telemetry) == 1
    body = telemetry[0][2]
    assert body["battery_mv"] == 2950
    assert body["rssi"] == -71
    assert body["firmware"] == "2.4.1"
    assert dt_util.parse_datetime(body["last_seen"]) is not None

    # Next round after X-Refresh-After: the ETag goes along, a 304 uploads nothing.
    aioclient_mock.clear_requests()
    mock_erp(aioclient_mock)
    mock_frame(aioclient_mock, status=304)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=601))
    await hass.async_block_till_done(wait_background_tasks=True)

    frame_call = calls_to(aioclient_mock, "GET", "/api/homeassistant/displays/12/frame.png")[0]
    assert frame_call[3]["If-None-Match"] == '"etag1"'
    assert len(uploads) == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_failed_upload_is_retried_with_the_full_frame(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tmp_path
) -> None:
    hass.config.media_dirs = {"local": str(tmp_path)}
    mock_erp(aioclient_mock)
    mock_frame(aioclient_mock)
    od_entry = MockConfigEntry(domain="opendisplay")
    od_entry.add_to_hass(hass)
    attempts: list[ServiceCall] = []

    async def failing_upload(call: ServiceCall) -> None:
        attempts.append(call)
        raise RuntimeError("panel out of range")

    hass.services.async_register("opendisplay", "upload_image", failing_upload)
    device_id = panel(hass, od_entry)

    entry = make_entry(options={CONF_DISPLAYS: {"12": device_id}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(attempts) == 1

    aioclient_mock.clear_requests()
    mock_erp(aioclient_mock)
    mock_frame(aioclient_mock)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=301))
    await hass.async_block_till_done(wait_background_tasks=True)

    # No ETag after a failed upload: the ERP sends the whole frame again.
    frame_call = calls_to(aioclient_mock, "GET", "/api/homeassistant/displays/12/frame.png")[0]
    assert "If-None-Match" not in frame_call[3]
    assert len(attempts) == 2

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_no_telemetry_without_a_sign_of_the_panel(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tmp_path
) -> None:
    hass.config.media_dirs = {"local": str(tmp_path)}
    mock_erp(aioclient_mock)
    mock_frame(aioclient_mock, status=304)
    od_entry, _ = fake_opendisplay(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=od_entry.entry_id, identifiers={("opendisplay", "C3D4")}
    )

    entry = make_entry(options={CONF_DISPLAYS: {"12": device.id}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    # A 304 is no upload and there are no sensor readings: the ERP must not
    # count Home Assistant being up as the panel being alive.
    assert not calls_to(aioclient_mock, "POST", "/api/homeassistant/displays/12/telemetry")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
