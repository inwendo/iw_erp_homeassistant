"""Offer Home Assistant locks to the inwendo ERP (opt-in).

The locks selected in the options are pushed to ``/api/homeassistant/ha_locks``
together with this instance's webhook URL and a shared secret; every state
change is pushed again. The ERP shows them as a smart lock connection and,
once an administrator synced its devices, sends lock commands to the webhook.
Commands are only executed when

* the HMAC-SHA256 signature (header ``X-IW-Signature``) matches the secret,
* the command is fresh (``ts``) and its ``nonce`` was not seen before,
* the entity is still one of the offered locks.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

from homeassistant.components.lock import LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES
from homeassistant.core import Context, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import instance_id
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .api import api_request_json
from .const import (
    CONF_EXPOSE_HA_LOCKS,
    CONF_HOST,
    CONF_LOCK_SECRET,
    CONF_TOKEN,
    DOMAIN,
    LOCK_COMMAND_MAX_AGE,
    LOCK_COMMAND_TYPE,
    UNIVERSAL_WEBHOOK_ID,
)

_LOGGER = logging.getLogger(__name__)

HA_LOCKS_PATH = "/api/homeassistant/ha_locks"


def exposed_entity_ids(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """Return the lock entities the user chose to offer, minus this integration's own locks."""
    registry = er.async_get(hass)
    result = []
    for entity_id in entry.options.get(CONF_EXPOSE_HA_LOCKS, []) or []:
        if not entity_id.startswith("lock."):
            continue
        reg_entry = registry.async_get(entity_id)
        if reg_entry is not None and reg_entry.platform == DOMAIN:
            # ERP locks imported into Home Assistant are never offered back.
            continue
        result.append(entity_id)
    return result


async def async_unregister_ha_locks(
    hass: HomeAssistant, host: str, token: str, secret: str
) -> bool:
    """Tell the ERP that this instance no longer offers locks."""
    session = async_get_clientsession(hass)
    _data, error = await api_request_json(
        session,
        "DELETE",
        f"{host}{HA_LOCKS_PATH}",
        token,
        _LOGGER,
        "Withdraw Home Assistant locks from ERP",
        json_body={"instance_id": await instance_id.async_get(hass), "secret": secret},
        level=logging.WARNING,
    )
    return error is None


class HaLockExporter:
    """Keeps the ERP informed about the offered locks and executes its commands."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._secret: str = entry.data[CONF_LOCK_SECRET]
        self._instance_id: str | None = None
        self._unsub_state = None
        self._seen_nonces: dict[str, float] = {}
        self._debouncer = Debouncer(
            hass, _LOGGER, cooldown=2.0, immediate=False, function=self.async_push
        )

    @property
    def entity_ids(self) -> list[str]:
        return exposed_entity_ids(self.hass, self.entry)

    async def async_start(self) -> None:
        """Push the offered locks and follow their state changes."""
        self._instance_id = await instance_id.async_get(self.hass)
        self._unsub_state = async_track_state_change_event(
            self.hass, self.entity_ids, self._state_changed
        )
        await self.async_push()

    @callback
    def async_stop(self) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        self._debouncer.async_cancel()

    async def async_unregister(self) -> bool:
        return await async_unregister_ha_locks(
            self.hass, self.entry.data[CONF_HOST], self.entry.data[CONF_TOKEN], self._secret
        )

    @callback
    def _state_changed(self, event: Event[EventStateChangedData]) -> None:
        self.hass.async_create_task(self._debouncer.async_call())

    def _lock_payload(self) -> list[dict[str, Any]]:
        locks = []
        for entity_id in self.entity_ids:
            state = self.hass.states.get(entity_id)
            features = state.attributes.get(ATTR_SUPPORTED_FEATURES, 0) if state else 0
            locks.append(
                {
                    "entity_id": entity_id,
                    "name": (state.name if state else entity_id)[:190],
                    "state": state.state if state else "unavailable",
                    "supports_open": bool(features & LockEntityFeature.OPEN),
                }
            )
        return locks

    async def async_push(self) -> bool:
        """Send the offered locks and their states to the ERP."""
        try:
            base_url = get_url(
                self.hass,
                allow_internal=False,
                allow_ip=False,
                require_ssl=True,
                prefer_cloud=True,
            )
        except NoURLAvailableError:
            _LOGGER.error(
                "Cannot offer locks to the ERP: Home Assistant has no external https URL "
                "(Settings > System > Network or Home Assistant Cloud). The ERP must be able "
                "to reach the webhook to send lock commands"
            )
            return False

        session = async_get_clientsession(self.hass)
        data, error = await api_request_json(
            session,
            "POST",
            f"{self.entry.data[CONF_HOST]}{HA_LOCKS_PATH}",
            self.entry.data[CONF_TOKEN],
            _LOGGER,
            "Offer Home Assistant locks to ERP",
            json_body={
                "instance_id": self._instance_id,
                "name": self.hass.config.location_name or "Home Assistant",
                "webhook_url": f"{base_url}/api/webhook/{UNIVERSAL_WEBHOOK_ID}",
                "secret": self._secret,
                "locks": self._lock_payload(),
            },
            level=logging.WARNING,
        )
        if error is None:
            _LOGGER.debug("Offered locks to ERP: %s", data)
        return error is None

    def verify_signature(self, body: bytes, signature: str | None) -> bool:
        if not signature:
            return False
        expected = hmac.new(self._secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature.strip().lower())

    async def async_handle_command(self, body: bytes, signature: str | None) -> tuple[int, dict[str, Any]]:
        """Execute a signed lock command. Returns (http status, json body)."""
        if not self.verify_signature(body, signature):
            return 401, {"success": False, "error": "invalid_signature"}
        try:
            payload = json.loads(body)
        except ValueError:
            return 400, {"success": False, "error": "invalid_json"}

        now = time.time()
        ts = payload.get("ts")
        nonce = payload.get("nonce")
        if not isinstance(ts, (int, float)) or abs(now - ts) > LOCK_COMMAND_MAX_AGE:
            return 400, {"success": False, "error": "expired"}
        if not isinstance(nonce, str) or not nonce or nonce in self._seen_nonces:
            return 400, {"success": False, "error": "replayed"}
        self._seen_nonces = {
            n: t for n, t in self._seen_nonces.items() if now - t <= 2 * LOCK_COMMAND_MAX_AGE
        }
        self._seen_nonces[nonce] = now

        if payload.get("type") != LOCK_COMMAND_TYPE or payload.get("instance_id") != self._instance_id:
            return 400, {"success": False, "error": "invalid_command"}

        entity_id = payload.get("entity_id")
        if entity_id not in self.entity_ids:
            return 403, {"success": False, "error": "not_exposed"}
        state = self.hass.states.get(entity_id)
        if state is None or state.state == "unavailable":
            return 409, {"success": False, "error": "unavailable"}

        command = payload.get("command")
        if command == "lock":
            service = "lock"
        elif command == "open":
            features = state.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
            service = "open" if features & LockEntityFeature.OPEN else "unlock"
        else:
            return 400, {"success": False, "error": "unknown_command"}

        _LOGGER.info("ERP lock command: %s %s", service, entity_id)
        try:
            await self.hass.services.async_call(
                "lock",
                service,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
                context=Context(),
            )
        except Exception as err:  # noqa: BLE001 - reported back to the ERP
            _LOGGER.warning("ERP lock command %s %s failed: %s", service, entity_id, err)
            return 200, {"success": False, "error": type(err).__name__}

        new_state = self.hass.states.get(entity_id)
        return 200, {"success": True, "state": new_state.state if new_state else "unknown"}
