"""HTML Artifacts: a place for Claude-generated, editable HTML apps in Home Assistant.

Pages published by Claude (over MCP or REST) are stored here, shown in a
sidebar panel or as a dashboard card inside a sandboxed iframe, and get a small
`window.ha` API: shared key/value state, server-side timers, entity access and
phone notifications.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import llm
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    ACTION_PREFIX,
    DOMAIN,
    JS_FILE,
    PANEL_ELEMENT,
    PANEL_URL_PATH,
    STATIC_URL,
    VERSION,
)
from .core import ArtifactError, ArtifactStore, TimerEngine
from .http import async_register_http
from .llm_api import ArtifactsAPI
from .notifier import TimerNotifier
from .websocket_api import async_register_websocket

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["update"]


@dataclass
class Runtime:
    artifacts: ArtifactStore
    timers: TimerEngine
    notifier: TimerNotifier

    def delete_artifact(self, artifact_id: str) -> None:
        self.artifacts.delete(artifact_id)
        self.timers.delete_for_artifact(artifact_id)


def make_scheduler(hass: HomeAssistant):
    """Scheduler for TimerEngine: run `callback_fn` in the event loop after `delay`.

    The inner function must be a @callback: Home Assistant runs plain functions
    in an executor thread, where touching the bus or services is not allowed.
    """

    def scheduler(delay: float, callback_fn):
        @callback
        def fire(_now) -> None:
            callback_fn()

        return async_call_later(hass, delay, fire)

    return scheduler


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """One-time registration of the API surface and static files."""
    hass.data.setdefault(DOMAIN, {})
    async_register_websocket(hass)
    async_register_http(hass)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "frontend"), False)]
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    artifacts = ArtifactStore(Store(hass, 1, f"{DOMAIN}.artifacts"))
    await artifacts.async_load()

    timers = TimerEngine(Store(hass, 1, f"{DOMAIN}.timers"), time.time, make_scheduler(hass))
    await timers.async_load()

    notifier = TimerNotifier(hass, artifacts, entry)
    entry.async_on_unload(timers.add_listener(notifier.handle))

    @callback
    def on_notification_action(event: Event) -> None:
        parts = str(event.data.get("action", "")).split("|")
        if len(parts) != 3 or parts[0] != ACTION_PREFIX:
            return
        _, timer_id, verb = parts
        try:
            if verb == "extend":
                timers.extend(timer_id, 60)
            elif verb == "pause":
                timers.pause(timer_id)
            elif verb == "resume":
                timers.resume(timer_id)
            elif verb == "next":
                timers.skip(timer_id)
        except ArtifactError as err:
            _LOGGER.debug("Ignoring notification action %s: %s", parts, err.message)

    entry.async_on_unload(hass.bus.async_listen("mobile_app_notification_action", on_notification_action))

    hass.data[DOMAIN]["runtime"] = Runtime(artifacts, timers, notifier)

    url = f"{STATIC_URL}/{JS_FILE}?v={VERSION}"
    hass.data[DOMAIN]["js_url"] = url
    await panel_custom.async_register_panel(
        hass,
        webcomponent_name=PANEL_ELEMENT,
        frontend_url_path=PANEL_URL_PATH,
        module_url=url,
        sidebar_title="Artifacts",
        sidebar_icon="mdi:language-html5",
        require_admin=False,
        config={},
        embed_iframe=False,
    )
    frontend.add_extra_js_url(hass, url)  # makes the dashboard card available

    api = ArtifactsAPI(hass)
    try:
        unregister = llm.async_register_api(hass, api)
    except Exception as err:  # noqa: BLE001 - e.g. already registered after a reload
        _LOGGER.debug("LLM API registration skipped: %s", err)
        unregister = None
    if callable(unregister):
        entry.async_on_unload(unregister)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    runtime: Runtime | None = hass.data[DOMAIN].pop("runtime", None)
    if runtime is not None:
        runtime.timers.async_shutdown()
    frontend.async_remove_panel(hass, PANEL_URL_PATH)
    url = hass.data[DOMAIN].pop("js_url", None)
    if url:
        frontend.remove_extra_js_url(hass, url)
    return True
