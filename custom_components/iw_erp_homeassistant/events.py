"""Access and check-in events from the ERP (opt-in: "Access and check-in events").

With the option on, the webhook registration asks the ERP for
``"events": ["access", "check_in"]``. The ERP then also posts every smart lock
log entry and every check-in / check-out of an attendance list to the
integration's webhook. These are no bookings: the webhook handler passes them
here and they become Home Assistant events:

``iw_erp_access``
    smart_lock_id, smart_lock_name, action (unlock, lock, access_code,
    access_denied), success, person_id, person_name, source, at

``iw_erp_check_in``
    attendance_list_id, attendance_list_name, person_id, person_name,
    checked_in (False on check-out), at

Both also carry ``config_entry_id``, ``erp_entity_id`` and ``verified`` (the
request carried this installation's webhook token). Bookings are not sent as
events: the calendar trigger of Home Assistant covers their start and end.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import CONF_LOCK_SECRET, EVENT_ACCESS, EVENT_CHECK_IN, SIGNAL_ACCESS

_LOGGER = logging.getLogger(__name__)

# iw_entity_short_class values. The ERP's short name lookup returns the first
# alias of a class, so accept every alias it knows.
BOOKING_CLASSES = frozenset({"base_booking", "booking", "basebooking", "event_base_booking"})
ACCESS_CLASSES = frozenset({"smart_lock_log", "event_smart_lock_log", "smartlocklog"})
CHECK_IN_CLASSES = frozenset(
    {"attendance_list_entry", "attendancelistentry", "event_attendance_list_entry"}
)

KIND_BOOKING = "booking"
KIND_ACCESS = "access"
KIND_CHECK_IN = "check_in"
KIND_OTHER = "other"

# Smart lock log actions that are no access (a polled state).
SKIPPED_ACCESS_ACTIONS = frozenset({"status"})

ACCESS_FIELDS = (
    "smart_lock_id",
    "smart_lock_name",
    "action",
    "success",
    "person_id",
    "person_name",
    "source",
    "at",
)
CHECK_IN_FIELDS = (
    "attendance_list_id",
    "attendance_list_name",
    "person_id",
    "person_name",
    "checked_in",
    "at",
)


def webhook_token(entry) -> str:
    """The token this installation registers its webhook with (derived, not stored)."""
    secret = entry.data.get(CONF_LOCK_SECRET) or entry.entry_id
    return hmac.new(secret.encode(), b"iw_erp_webhook", hashlib.sha256).hexdigest()


def summary_of(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The event summary of a webhook (``webhook_data``; ``ha_event`` in early drafts)."""
    for key in ("webhook_data", "ha_event"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return None


def classify(payload: dict[str, Any]) -> str:
    """What a webhook is about: booking, access, check_in or other."""
    short = str(payload.get("iw_entity_short_class") or "").strip().lower()
    long = str(payload.get("iw_entity_class") or "")
    summary = summary_of(payload)
    summary_type = summary.get("type") if summary else None

    if short in ACCESS_CLASSES or long.endswith("\\SmartLockLog") or summary_type == "access":
        return KIND_ACCESS
    if short in CHECK_IN_CLASSES or long.endswith("\\AttendanceListEntry") or summary_type == "check_in":
        return KIND_CHECK_IN
    if short in BOOKING_CLASSES or long.endswith("\\BaseBooking") or (not short and not long):
        # Without any class it is a booking notification of an older ERP.
        return KIND_BOOKING
    return KIND_OTHER


def _bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def build_event_data(kind: str, payload: dict[str, Any], entry_id: str | None, verified: bool) -> dict[str, Any] | None:
    """Event data for the bus, or None when there is nothing to fire."""
    summary = summary_of(payload)
    if summary is None or payload.get("iw_action") == "delete":
        # A deleted log / attendance entry carries no summary: nothing happened.
        return None
    fields = ACCESS_FIELDS if kind == KIND_ACCESS else CHECK_IN_FIELDS
    data: dict[str, Any] = {key: summary.get(key) for key in fields}
    if kind == KIND_ACCESS:
        if data["action"] in SKIPPED_ACCESS_ACTIONS:
            return None
        data["success"] = _bool(data["success"])
    else:
        checked_in = _bool(data["checked_in"])
        if checked_in is None:
            checked_in = payload.get("iw_action") == "create"
        data["checked_in"] = checked_in
    data["erp_entity_id"] = payload.get("iw_entity_id")
    data["erp_action"] = payload.get("iw_action")
    data["config_entry_id"] = entry_id
    data["verified"] = verified
    return data


def async_fire(hass: HomeAssistant, kind: str, payload: dict[str, Any], entries: list, verified: bool) -> bool:
    """Fire the Home Assistant event for an access / check-in webhook.

    ``entries`` are the loaded config entries with the option on. With exactly
    one the event names it; with several (several ERPs on one Home Assistant)
    the webhook cannot tell which ERP sent it and ``config_entry_id`` is None.
    """
    if not entries:
        _LOGGER.debug("Ignored %s webhook: no config entry has access and check-in events on", kind)
        return False
    entry_id = entries[0].entry_id if len(entries) == 1 else None
    data = build_event_data(kind, payload, entry_id, verified)
    if data is None:
        return False
    event_type = EVENT_ACCESS if kind == KIND_ACCESS else EVENT_CHECK_IN
    _LOGGER.debug("Firing %s: %s", event_type, data)
    hass.bus.async_fire(event_type, data)
    if kind == KIND_ACCESS:
        for entry in entries:
            async_dispatcher_send(hass, SIGNAL_ACCESS, entry.entry_id, data)
    return True

