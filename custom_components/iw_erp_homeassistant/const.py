"""Constants for the inwendo ERP / vynst integration."""

DOMAIN = "iw_erp_homeassistant"

# Configuration constants (config entry data)
CONF_HOST = "host"
CONF_TOKEN = "api_key"
# Shared secret the ERP signs lock commands with (generated on first opt-in).
CONF_LOCK_SECRET = "lock_secret"

# Options (all opt-in, off by default)
CONF_IMPORT_ERP_LOCKS = "import_erp_locks"
CONF_EXPOSE_HA_LOCKS = "expose_ha_locks"
# ERP room display id -> OpenDisplay device id (see display.py)
CONF_DISPLAYS = "displays"
# Image entities for every ERP room display, not only the ones on an OpenDisplay panel.
CONF_DISPLAY_IMAGES = "display_images"
# Occupancy sensors per bookable (see occupancy.py)
CONF_OCCUPANCY = "occupancy_sensors"
# Access (smart lock log) and check-in events from the ERP webhook (see events.py)
CONF_ACCESS_EVENTS = "access_events"
# ERP usage counters as sensors, and Home Assistant meters reported to them (see usage_counters.py)
CONF_USAGE_COUNTERS = "usage_counters"
# ERP usage counter id -> Home Assistant sensor entity id
CONF_USAGE_COUNTER_MAP = "usage_counter_map"

# ERP room displays on OpenDisplay panels
OPENDISPLAY_DOMAIN = "opendisplay"
OPENDISPLAY_UPLOAD_SERVICE = "upload_image"
# Sub folder of the local media folder the frames are stored in for the upload.
DISPLAY_MEDIA_FOLDER = "iw_erp_displays"

# The one webhook of this integration: /api/webhook/iw_erp_homeassistant.
# Receives booking notifications and (when HA locks are offered) signed lock commands.
UNIVERSAL_WEBHOOK_ID = DOMAIN

# Lock commands from the ERP
LOCK_COMMAND_TYPE = "iw_lock_command"
SIGNATURE_HEADER = "X-IW-Signature"
# Commands older than this (seconds) are rejected, a nonce is accepted once.
LOCK_COMMAND_MAX_AGE = 120

# Header the ERP sends with every webhook when the registration carried a webhook_token.
WEBHOOK_TOKEN_HEADER = "X-IW-Webhook-Token"

# Events fired on the Home Assistant bus (access and check-in, opt-in)
EVENT_ACCESS = "iw_erp_access"
EVENT_CHECK_IN = "iw_erp_check_in"
# Signal sent with (entry_id, event data) after an access event, for the "last access" sensors
SIGNAL_ACCESS = f"{DOMAIN}_access"

# Feature names: keys of the "feature disabled" bookkeeping and of the missing scope repair issues.
FEATURE_OCCUPANCY = "occupancy"
FEATURE_USAGE_COUNTERS = "usage_counters"
FEATURE_ACCESS_EVENTS = "access_events"
FEATURE_CHECK_ACCESS = "check_access"
FEATURE_TICKET = "ticket"
FEATURE_DISPLAYS = "displays"
# The API key scope each feature needs (shown in the repair issue).
FEATURE_SCOPES = {
    FEATURE_OCCUPANCY: "Location (read)",
    FEATURE_USAGE_COUNTERS: "Location (read + write)",
    FEATURE_ACCESS_EVENTS: "Event Booking (write)",
    FEATURE_CHECK_ACCESS: "Event (write)",
    FEATURE_TICKET: "Ticket (write)",
    FEATURE_DISPLAYS: "Location (read)",
}
# The ERP setting that switches a feature on (shown in logs and errors).
FEATURE_SETTINGS = {
    FEATURE_OCCUPANCY: "integration_occupancy_api_enabled",
    FEATURE_USAGE_COUNTERS: "integration_usage_counter_api_enabled",
    FEATURE_ACCESS_EVENTS: "integration_ha_access_events_enabled",
    FEATURE_CHECK_ACCESS: "integration_ha_access_check_enabled",
    FEATURE_TICKET: "integration_ha_ticket_enabled",
}
