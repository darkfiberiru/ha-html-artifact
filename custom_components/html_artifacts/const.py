"""Constants for the HTML Artifacts integration."""
from __future__ import annotations

DOMAIN = "html_artifacts"
VERSION = "0.2.2"

PANEL_URL_PATH = "html-artifacts"
PANEL_ELEMENT = "html-artifacts-panel"
STATIC_URL = "/html_artifacts_static"
JS_FILE = "html-artifacts.js"

CONF_NOTIFY_TARGETS = "notify_targets"
CONF_LIVE = "live_notification"
CONF_REPO = "github_repo"
CONF_GITHUB_TOKEN = "github_token"
CONF_AUTO_RESTART = "auto_restart"
CONF_FORCE_SELF_UPDATE = "force_self_update"
DEFAULT_REPO = "darkfiberiru/ha-html-artifact"

EVENT_STEP = "html_artifacts_timer_step"
EVENT_FINISHED = "html_artifacts_timer_finished"

# Notification action ids look like "HTMLART|<timer_id>|<verb>".
ACTION_PREFIX = "HTMLART"

LLM_API_ID = "html_artifacts"
