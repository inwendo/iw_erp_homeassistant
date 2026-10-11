"""Repair issues and the quiet handling of ERP functions that are switched off.

Every new function of the integration is opt-in on the Home Assistant side and
behind a feature flag in the ERP. When the ERP says "feature disabled" (or an
older ERP does not know the endpoint) the function just stays away: one info
line per config entry and function, no entities, no reauth, no error spam.

Repair issues (Settings > System > Repairs) are raised for things the user has
to fix and cleared as soon as they are fixed:

* ``no_external_url``: the ERP cannot push changes because Home Assistant has
  no external https URL; the integration polls instead.
* ``api_key_rejected``: the ERP rejected the API key (in addition to reauth).
* ``missing_scope``: an enabled option needs a scope the API key lacks (403).
"""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .api import ApiError, is_feature_disabled, is_missing_scope
from .const import DOMAIN, FEATURE_SCOPES, FEATURE_SETTINGS

_LOGGER = logging.getLogger(__name__)

ISSUE_NO_EXTERNAL_URL = "no_external_url"
ISSUE_API_KEY_REJECTED = "api_key_rejected"
ISSUE_MISSING_SCOPE = "missing_scope"


def _issue_id(key: str, entry_id: str, feature: str | None = None) -> str:
    return f"{key}_{entry_id}_{feature}" if feature else f"{key}_{entry_id}"


def async_raise(
    hass: HomeAssistant,
    key: str,
    entry_id: str,
    title: str,
    feature: str | None = None,
    severity: ir.IssueSeverity = ir.IssueSeverity.WARNING,
) -> None:
    """Create (or refresh) a repair issue of a config entry."""
    placeholders = {"title": title}
    if feature:
        placeholders["feature"] = feature
        placeholders["scope"] = FEATURE_SCOPES.get(feature, "-")
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(key, entry_id, feature),
        is_fixable=False,
        is_persistent=False,
        severity=severity,
        translation_key=key,
        translation_placeholders=placeholders,
    )


def async_clear(hass: HomeAssistant, key: str, entry_id: str, feature: str | None = None) -> None:
    """Delete a repair issue once its cause is gone (no-op when there is none)."""
    ir.async_delete_issue(hass, DOMAIN, _issue_id(key, entry_id, feature))


def async_clear_all(hass: HomeAssistant, entry_id: str) -> None:
    """Remove every issue of a config entry (entry removed)."""
    registry = ir.async_get(hass)
    for domain, issue_id in list(registry.issues):
        if domain == DOMAIN and entry_id in issue_id:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


class FeatureTracker:
    """Per config entry: which ERP functions are off, and the missing scope issues."""

    def __init__(self, hass: HomeAssistant, entry_id: str, title: str) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.title = title
        # feature -> True (available), False (switched off in the ERP)
        self.available: dict[str, bool] = {}
        self._logged_disabled: set[str] = set()

    def ok(self, feature: str) -> None:
        """The function answered: clear its scope issue, log a comeback once."""
        if self.available.get(feature) is False:
            _LOGGER.info("ERP function %s is available again", feature)
        self.available[feature] = True
        self._logged_disabled.discard(feature)
        async_clear(self.hass, ISSUE_MISSING_SCOPE, self.entry_id, feature)

    def handle_error(self, feature: str, error: ApiError) -> bool:
        """Classify a failed request. Returns True when it was handled quietly.

        Handled quietly: "feature disabled" (info once) and a missing scope
        (repair issue). Anything else is the caller's business (auth, outage).
        """
        if is_feature_disabled(error):
            self.available[feature] = False
            if feature not in self._logged_disabled:
                self._logged_disabled.add(feature)
                setting = FEATURE_SETTINGS.get(feature)
                _LOGGER.info(
                    "ERP function %s is not available (switched off in the ERP%s, or the ERP "
                    "version does not offer it yet); the related Home Assistant functions stay inactive",
                    feature,
                    f": setting {setting}" if setting else "",
                )
            return True
        if is_missing_scope(error):
            async_raise(self.hass, ISSUE_MISSING_SCOPE, self.entry_id, self.title, feature)
            return True
        return False


def async_api_key_rejected(hass: HomeAssistant, entry) -> None:
    """The ERP rejected the API key: a repair issue next to the reauth flow."""
    async_raise(hass, ISSUE_API_KEY_REJECTED, entry.entry_id, entry.title, severity=ir.IssueSeverity.ERROR)
