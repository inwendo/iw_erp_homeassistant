"""ERP room displays on OpenDisplay panels (opt-in: "Deliver ERP room displays").

OpenDisplay panels (https://opendisplay.org) are e-paper receivers that are pushed
to over Bluetooth LE by a sender next to them. With the OpenDisplay integration
installed, Home Assistant is that sender - so this integration only has to bring
the picture: for every ERP room display of the type "OpenDisplay" that the user
assigned to an OpenDisplay device, it

1. fetches the frame from ``/api/homeassistant/displays/{id}/frame.png`` with
   ``If-None-Match``, so an unchanged sign costs one 304 and no Bluetooth upload,
2. stores it in the local media folder and hands it to ``opendisplay.upload_image``
   (dithering to the panel's palette is done there),
3. reports the panel's battery, signal, firmware and last contact as Home Assistant
   sees them to ``/api/homeassistant/displays/{id}/telemetry``, which feeds the
   ERP's battery and offline maintenance reports,
4. waits as long as the ERP says (``X-Refresh-After``: the configured interval or
   the next booking boundary, whichever comes first).

The ERP renders everything; Home Assistant knows no rooms or bookings.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .api import (
    AUTH_ERROR_KEYS,
    api_get_json,
    api_request_json,
    classify_http_status,
    client_timeout,
    extract_erp_error_headers,
    http_status_error,
    log_api_error,
    read_body_snippet,
)
from .const import (
    CONF_DISPLAYS,
    CONF_HOST,
    CONF_TOKEN,
    DISPLAY_MEDIA_FOLDER,
    OPENDISPLAY_DOMAIN,
    OPENDISPLAY_UPLOAD_SERVICE,
)

_LOGGER = logging.getLogger(__name__)

# Bounds for the wait between two fetches, whatever the ERP sends.
MIN_INTERVAL = 60
MAX_INTERVAL = 86400
# After a failed fetch or upload: try again soon, but do not hammer a dead panel.
RETRY_INTERVAL = 300
# Sensors of an OpenDisplay device, by the translation_key the integration gives them.
TELEMETRY_KEYS = {"battery_voltage", "rssi", "last_seen"}


async def async_fetch_erp_displays(hass: HomeAssistant, host: str, token: str) -> list[dict[str, Any]] | None:
    """The ERP's OpenDisplay receivers, or None when the ERP cannot tell."""
    data, error = await api_get_json(
        async_get_clientsession(hass),
        f"{host}/api/homeassistant/displays",
        token,
        _LOGGER,
        "Fetch ERP room displays",
        timeout=15,
    )
    if error is not None or not isinstance(data, list):
        return None
    return [d for d in data if isinstance(d, dict) and d.get("id") is not None]


def configured_displays(options: dict[str, Any]) -> dict[str, str]:
    """ERP display id -> OpenDisplay device id, from the options."""
    mapping = options.get(CONF_DISPLAYS) or {}
    if not isinstance(mapping, dict):
        return {}
    return {str(k): str(v) for k, v in mapping.items() if k and v}


def _clamp(seconds: int) -> int:
    return max(MIN_INTERVAL, min(MAX_INTERVAL, seconds))


@dataclass
class _DisplayState:
    display_id: str
    device_id: str
    etag: str | None = None
    unsub: CALLBACK_TYPE | None = None
    # A finished Bluetooth upload proves the panel was there at that moment.
    last_upload: datetime | None = None


class ErpDisplaySender:
    """Keeps the assigned OpenDisplay panels showing their ERP room display."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.host: str = entry.data[CONF_HOST]
        self.token: str = entry.data[CONF_TOKEN]
        self._entry = entry
        self._displays = {
            display_id: _DisplayState(display_id, device_id)
            for display_id, device_id in configured_displays(entry.options).items()
        }
        self._stopped = False

    async def async_start(self) -> None:
        for state in self._displays.values():
            await self._async_cycle(state)

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        for state in self._displays.values():
            if state.unsub is not None:
                state.unsub()
                state.unsub = None

    def _schedule(self, state: _DisplayState, seconds: int) -> None:
        if self._stopped:
            return

        @callback
        def _fire(_now: datetime) -> None:
            state.unsub = None
            self._entry.async_create_background_task(
                self.hass, self._async_cycle(state), f"iw_erp display {state.display_id}"
            )

        state.unsub = async_call_later(self.hass, _clamp(seconds), _fire)

    async def _async_cycle(self, state: _DisplayState) -> None:
        """One round for one panel: frame, upload, telemetry, next appointment."""
        try:
            wait = await self._async_deliver(state)
        except ConfigEntryAuthFailed:
            self._entry.async_start_reauth(self.hass)
            return
        except Exception:  # noqa: BLE001 - one panel must never stop the others
            _LOGGER.exception("Delivering ERP room display %s failed", state.display_id)
            wait = RETRY_INTERVAL
        await self._async_report_telemetry(state)
        self._schedule(state, wait)

    async def _async_deliver(self, state: _DisplayState) -> int:
        """Fetch the frame; upload it when it changed. Returns seconds until the next round."""
        url = f"{self.host}/api/homeassistant/displays/{state.display_id}/frame.png"
        headers = {"x-iw-jwt-token": self.token}
        if state.etag:
            headers["If-None-Match"] = state.etag

        session = async_get_clientsession(self.hass)
        async with session.get(url, headers=headers, timeout=client_timeout(30)) as resp:
            refresh = _parse_int(resp.headers.get("X-Refresh-After"), RETRY_INTERVAL)
            if resp.status == 304:
                return refresh
            if resp.status != 200:
                erp_code, erp_detail = extract_erp_error_headers(resp)
                log_api_error(
                    _LOGGER,
                    f"Fetch frame of ERP room display {state.display_id}",
                    url,
                    http_status_error(resp),
                    status=resp.status,
                    body_snippet=await read_body_snippet(resp),
                    erp_code=erp_code,
                    erp_detail=erp_detail,
                    level=logging.WARNING,
                )
                if classify_http_status(resp.status) in AUTH_ERROR_KEYS:
                    raise ConfigEntryAuthFailed("ERP rejected the API key for room displays")
                return RETRY_INTERVAL
            body = await resp.read()
            etag = resp.headers.get("ETag")

        if not self.hass.services.has_service(OPENDISPLAY_DOMAIN, OPENDISPLAY_UPLOAD_SERVICE):
            _LOGGER.warning(
                "ERP room display %s: the OpenDisplay integration is not loaded, nothing uploaded",
                state.display_id,
            )
            return RETRY_INTERVAL

        media_id = await self.hass.async_add_executor_job(self._write_media, state.display_id, body)
        if media_id is None:
            return RETRY_INTERVAL

        await self.hass.services.async_call(
            OPENDISPLAY_DOMAIN,
            OPENDISPLAY_UPLOAD_SERVICE,
            {
                "device_id": state.device_id,
                "image": {"media_content_id": media_id, "media_content_type": "image/png"},
            },
            blocking=True,
        )
        # Only after the upload went through: a failed upload must be retried with
        # the full frame, not answered with a 304.
        state.etag = etag
        state.last_upload = dt_util.utcnow()
        return refresh

    def _write_media(self, display_id: str, body: bytes) -> str | None:
        """Store the frame where media_source finds it; returns its media id."""
        local = self.hass.config.media_dirs.get("local")
        if not local:
            _LOGGER.error(
                "ERP room display %s: no local media folder configured (media_dirs.local)", display_id
            )
            return None
        folder = os.path.join(local, DISPLAY_MEDIA_FOLDER)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{display_id}.png")
        tmp = f"{path}.tmp"
        with open(tmp, "wb") as handle:
            handle.write(body)
        os.replace(tmp, path)
        return f"media-source://media_source/local/{DISPLAY_MEDIA_FOLDER}/{display_id}.png"

    def collect_telemetry(self, device_id: str, last_upload: datetime | None = None) -> dict[str, Any]:
        """What Home Assistant knows about the panel, from the OpenDisplay sensors.

        ``last_seen`` is the panel's own last contact: the OpenDisplay "Last seen"
        sensor when it is enabled (it is not by default), else the last time the
        panel's battery or signal sensor got a reading from its advertisement, else
        the last finished upload - whichever is newest.
        """
        payload: dict[str, Any] = {}
        seen: list[datetime] = [last_upload] if last_upload is not None else []
        device = dr.async_get(self.hass).async_get(device_id)
        if device is not None and device.sw_version:
            payload["firmware"] = device.sw_version

        for entry in er.async_entries_for_device(er.async_get(self.hass), device_id):
            if entry.domain != "sensor" or entry.translation_key not in TELEMETRY_KEYS:
                continue
            state = self.hass.states.get(entry.entity_id)
            if state is None or state.state in ("unknown", "unavailable", ""):
                continue
            if entry.translation_key in ("battery_voltage", "rssi"):
                seen.append(state.last_reported)
            if entry.translation_key == "battery_voltage":
                unit = state.attributes.get("unit_of_measurement")
                value = _parse_float(state.state)
                if value is not None:
                    if unit == "V":
                        payload["battery_voltage"] = value
                    else:
                        payload["battery_mv"] = value
            elif entry.translation_key == "rssi":
                value = _parse_float(state.state)
                if value is not None:
                    payload["rssi"] = int(value)
            elif entry.translation_key == "last_seen":
                parsed = dt_util.parse_datetime(state.state)
                if parsed is not None:
                    seen.append(dt_util.as_utc(parsed))
        if seen:
            payload["last_seen"] = max(seen).isoformat()
        return payload

    async def _async_report_telemetry(self, state: _DisplayState) -> None:
        payload = self.collect_telemetry(state.device_id, state.last_upload)
        if "last_seen" not in payload:
            # Without any sign of the panel the ERP would count this round as the
            # panel being alive; better to say nothing than the wrong thing.
            return
        await api_request_json(
            async_get_clientsession(self.hass),
            "POST",
            f"{self.host}/api/homeassistant/displays/{state.display_id}/telemetry",
            self.token,
            _LOGGER,
            f"Report telemetry of ERP room display {state.display_id}",
            json_body=payload,
            timeout=15,
            level=logging.WARNING,
        )


def _parse_int(value: str | None, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _parse_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
