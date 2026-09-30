"""ERP smart locks as Home Assistant lock entities (opt-in: "Import ERP smart locks").

Lists the smart locks the API key's user may use (``/api/homeassistant/smart_locks``),
polls their state and locks / opens them through the ERP, which checks the
access rights and writes its smart lock log.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import instance_id
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import AUTH_ERROR_KEYS, api_get_json, api_request_json
from .const import CONF_HOST, CONF_TOKEN, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Every status request goes to the lock vendor's API (Nuki, LOQED), so poll gently.
SCAN_INTERVAL = timedelta(minutes=10)
# After a command the motor needs a moment; ask for the real state afterwards.
REFRESH_AFTER_COMMAND = 15
PARALLEL_STATUS_REQUESTS = 4


class ErpLockCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Fetches the accessible ERP smart locks and their states."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_smart_locks",
            update_interval=SCAN_INTERVAL,
        )
        self.host: str = entry.data[CONF_HOST]
        self.token: str = entry.data[CONF_TOKEN]

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        session = async_get_clientsession(self.hass)
        locks, error = await api_get_json(
            session,
            f"{self.host}/api/homeassistant/smart_locks",
            self.token,
            _LOGGER,
            "Fetch ERP smart locks",
            timeout=15,
            params={"excludeHaInstanceId": await instance_id.async_get(self.hass)},
        )
        if error is not None:
            if error.key in AUTH_ERROR_KEYS:
                raise ConfigEntryAuthFailed(f"Cannot fetch smart locks ({error.key})")
            raise UpdateFailed(f"Cannot fetch smart locks ({error.key})")
        if not isinstance(locks, list):
            raise UpdateFailed("Unexpected smart lock list")

        semaphore = asyncio.Semaphore(PARALLEL_STATUS_REQUESTS)

        async def fetch_state(lock_id: str) -> str:
            async with semaphore:
                data, err = await api_get_json(
                    session,
                    f"{self.host}/api/homeassistant/smart_locks/{lock_id}/status",
                    self.token,
                    _LOGGER,
                    f"Fetch state of ERP smart lock {lock_id}",
                    timeout=20,
                )
            if err is not None or not isinstance(data, dict):
                return "unknown"
            return str(data.get("state", "unknown"))

        result: dict[str, dict[str, Any]] = {}
        for lock in locks:
            if isinstance(lock, dict) and lock.get("id") is not None:
                result[str(lock["id"])] = {
                    "name": lock.get("name") or f"Smart lock {lock['id']}",
                    "type": lock.get("type"),
                    "state": "unknown",
                }
        states = await asyncio.gather(*(fetch_state(lock_id) for lock_id in result))
        for lock_id, state in zip(result, states, strict=True):
            result[lock_id]["state"] = state
        return result

    async def async_command(self, lock_id: str, command: str) -> None:
        """Lock ("lock") or open ("unlock") a smart lock through the ERP."""
        data, error = await api_request_json(
            async_get_clientsession(self.hass),
            "POST",
            f"{self.host}/api/homeassistant/smart_locks/{lock_id}/{command}",
            self.token,
            _LOGGER,
            f"{command} ERP smart lock {lock_id}",
            timeout=30,
        )
        if error is not None:
            raise HomeAssistantError(f"ERP rejected {command} ({error.key})")
        if not isinstance(data, dict) or data.get("success") is not True:
            raise HomeAssistantError(f"The smart lock did not execute {command}")


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up lock entities for the ERP smart locks (only called when the import is enabled)."""
    coordinator: ErpLockCoordinator = entry.runtime_data.lock_coordinator
    known: set[str] = set()

    @callback
    def add_new_locks() -> None:
        new = [lock_id for lock_id in (coordinator.data or {}) if lock_id not in known]
        known.update(new)
        if new:
            async_add_entities(ErpSmartLock(coordinator, entry.entry_id, lock_id) for lock_id in new)

    add_new_locks()
    entry.async_on_unload(coordinator.async_add_listener(add_new_locks))


class ErpSmartLock(CoordinatorEntity[ErpLockCoordinator], LockEntity):
    """An ERP smart lock (Nuki, LOQED, ...)."""

    _attr_supported_features = LockEntityFeature.OPEN

    def __init__(self, coordinator: ErpLockCoordinator, entry_id: str, lock_id: str) -> None:
        super().__init__(coordinator)
        self._lock_id = lock_id
        self._attr_unique_id = f"{entry_id}-smart-lock-{lock_id}"
        self._optimistic_state: str | None = None

    @property
    def _lock(self) -> dict[str, Any] | None:
        return (self.coordinator.data or {}).get(self._lock_id)

    @property
    def available(self) -> bool:
        return super().available and self._lock is not None

    @property
    def name(self) -> str | None:
        lock = self._lock
        return lock["name"] if lock else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        lock = self._lock or {}
        return {"erp_smart_lock_id": self._lock_id, "provider": lock.get("type")}

    @property
    def _state(self) -> str | None:
        if self._optimistic_state is not None:
            return self._optimistic_state
        lock = self._lock
        state = lock["state"] if lock else None
        return None if state in (None, "unknown") else state

    @property
    def is_locked(self) -> bool | None:
        state = self._state
        return None if state is None else state == "locked"

    @property
    def is_open(self) -> bool | None:
        state = self._state
        return None if state is None else state == "open"

    @property
    def is_jammed(self) -> bool:
        return self._state == "jammed"

    @property
    def is_locking(self) -> bool:
        return self._state == "locking"

    @property
    def is_unlocking(self) -> bool:
        return self._state == "unlocking"

    @property
    def is_opening(self) -> bool:
        return self._state == "opening"

    @callback
    def _handle_coordinator_update(self) -> None:
        self._optimistic_state = None
        super()._handle_coordinator_update()

    async def _async_run(self, command: str, assumed_state: str) -> None:
        await self.coordinator.async_command(self._lock_id, command)
        self._optimistic_state = assumed_state
        self.async_write_ha_state()
        self.async_on_remove(
            async_call_later(self.hass, REFRESH_AFTER_COMMAND, self._async_refresh_later)
        )

    async def _async_refresh_later(self, _now) -> None:
        await self.coordinator.async_request_refresh()

    async def async_lock(self, **kwargs: Any) -> None:
        await self._async_run("lock", "locked")

    async def async_unlock(self, **kwargs: Any) -> None:
        # The ERP has one "open" command: it unlocks and pulls the latch (Nuki unlatch, LOQED open).
        await self._async_run("unlock", "unlocked")

    async def async_open(self, **kwargs: Any) -> None:
        await self._async_run("unlock", "open")
