"""Logbook entries for the access and check-in events of the ERP."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_ACCESS, EVENT_CHECK_IN

# Smart lock log actions as words.
ACTION_TEXT = {
    "unlock": "opened",
    "lock": "locked",
    "access_code": "used an access code at",
    "access_denied": "was denied access to",
}


def describe_access(data: dict[str, Any]) -> dict[str, str]:
    """Name and message of an iw_erp_access event."""
    person = data.get("person_name") or "Someone"
    lock = data.get("smart_lock_name") or f"smart lock {data.get('smart_lock_id')}"
    action = data.get("action")
    text = ACTION_TEXT.get(action, f"{action} at" if action else "used")
    message = f"{text} {lock}"
    if data.get("success") is False and action != "access_denied":
        message += " (failed)"
    if data.get("source"):
        message += f" via {data['source']}"
    return {LOGBOOK_ENTRY_NAME: person, LOGBOOK_ENTRY_MESSAGE: message}


def describe_check_in(data: dict[str, Any]) -> dict[str, str]:
    """Name and message of an iw_erp_check_in event."""
    person = data.get("person_name") or "Someone"
    target = data.get("attendance_list_name") or f"attendance list {data.get('attendance_list_id')}"
    verb = "checked in at" if data.get("checked_in") else "checked out of"
    return {LOGBOOK_ENTRY_NAME: person, LOGBOOK_ENTRY_MESSAGE: f"{verb} {target}"}


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Describe the integration's events for the logbook."""

    @callback
    def _access(event: Event) -> dict[str, str]:
        return describe_access(event.data)

    @callback
    def _check_in(event: Event) -> dict[str, str]:
        return describe_check_in(event.data)

    async_describe_event(DOMAIN, EVENT_ACCESS, _access)
    async_describe_event(DOMAIN, EVENT_CHECK_IN, _check_in)
