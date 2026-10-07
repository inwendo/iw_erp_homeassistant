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
