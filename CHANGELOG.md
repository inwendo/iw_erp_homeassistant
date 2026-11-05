# Changelog

## 1.5.0

- New blueprints **heat a room by its occupancy** and **light by room occupancy**: built on the occupancy sensors, so bookings made at short notice and cancelled bookings are handled (the calendar trigger of Home Assistant only sees events that existed about 15 minutes before their trigger time).
- A booking moved to another room is removed from the calendar of the old room.
- The integration is named **vynst (inwendo ERP)**; installation from the Home Assistant Community Store (HACS).
- All texts in English and German: the integration's translations name the product **vynst (inwendo ERP)** everywhere, the blueprints carry English and German names and descriptions, and the README is also in German (`README.de.md`). The hub device is now called „vynst (inwendo ERP) <host>“, so a new installation gets entity ids like `sensor.vynst_inwendo_erp_<host>_last_access`; existing entity ids stay.
- Tested end to end against a real Home Assistant 2026.11 (servercontrolpanel `e2e/scripts/start-home-assistant.sh`).

## 1.4.0

- Smart locks in both directions, occupancy sensors, access and check-in events, usage counters, room displays on OpenDisplay panels, services `check_access` and `create_ticket`, blueprints for preheating, booking light and frost protection.
