"""Phone notifications and bus events for server-side timers."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import (
    ACTION_PREFIX,
    CONF_LIVE,
    CONF_NOTIFY_TARGETS,
    EVENT_FINISHED,
    EVENT_STEP,
)
from .core import ArtifactStore

_LOGGER = logging.getLogger(__name__)

LIVE_CHANGES = {"start", "resume", "skip", "extend", "step", "pause"}
CLEAR_CHANGES = {"reset", "delete", "finish"}


def _action(timer_id: str, verb: str, title: str) -> dict[str, str]:
    return {"action": f"{ACTION_PREFIX}|{timer_id}|{verb}", "title": title}


class TimerNotifier:
    """Turns timer events into notifications on the user's phone."""

    def __init__(self, hass: HomeAssistant, artifacts: ArtifactStore, entry: ConfigEntry) -> None:
        self._hass = hass
        self._artifacts = artifacts
        self._entry = entry
        self._warned: set[str] = set()

    # -- helpers ---------------------------------------------------------

    def _targets(self, timer: dict) -> list[str]:
        raw = timer.get("notify_targets") or self._entry.options.get(CONF_NOTIFY_TARGETS) or []
        return [t.removeprefix("notify.") for t in raw]

    def _allowed(self, timer: dict) -> bool:
        return bool(self._artifacts.get_grants(timer.get("artifact_id", "")).get("notify"))

    @staticmethod
    def _tag(timer: dict) -> str:
        return f"htmlart_{timer['id']}"

    async def _send(self, targets: list[str], title: str, message: str, data: dict[str, Any], notification_id: str) -> None:
        if not targets:
            await self._hass.services.async_call(
                "persistent_notification",
                "create",
                {"title": title, "message": message, "notification_id": notification_id},
                blocking=False,
            )
            return
        for target in targets:
            try:
                await self._hass.services.async_call(
                    "notify",
                    target,
                    {"title": title, "message": message, "data": data},
                    blocking=False,
                )
            except Exception as err:  # noqa: BLE001
                if target not in self._warned:
                    self._warned.add(target)
                    _LOGGER.warning("Could not notify %s: %s", target, err)

    # -- entry point ---------------------------------------------------------

    @callback
    def handle(self, event: dict) -> None:
        """Listener for TimerEngine events (runs in the event loop)."""
        if event["type"] == "timer_fired":
            timer = event["timer"]
            self._hass.bus.async_fire(
                EVENT_STEP if event["kind"] == "step" else EVENT_FINISHED,
                {
                    "timer_id": timer["id"],
                    "artifact_id": timer["artifact_id"],
                    "label": timer["label"],
                    "step": (event.get("step") or {}).get("label"),
                    "index": event.get("index"),
                    "missed": event.get("missed", 0),
                },
            )
            if self._allowed(timer):
                self._hass.async_create_task(self._alert(event))
        elif event["type"] == "timer":
            timer = event["timer"]
            if not self._allowed(timer) or not self._entry.options.get(CONF_LIVE, True):
                return
            change = event.get("change")
            if change in LIVE_CHANGES:
                self._hass.async_create_task(self._live(timer))
            elif change in CLEAR_CHANGES:
                self._hass.async_create_task(self._clear(timer))

    # -- the three kinds of notification ---------------------------------------------

    async def _alert(self, event: dict) -> None:
        timer, step = event["timer"], event.get("step")
        if event["kind"] == "finished":
            message = (step or {}).get("label") or "Done"
            if step:
                message = f"Done: {message}"
        else:
            message = step["label"]
            if step.get("detail"):
                message += f" - {step['detail']}"
            if event.get("missed"):
                message += f" (+{event['missed']} earlier step(s) passed while away)"
        data = {
            # Android companion app
            "tag": f"{self._tag(timer)}_alert",
            "channel": "html_artifacts_alerts",
            "importance": "high",
            "priority": "high",
            "ttl": 0,
            # iOS companion app
            "push": {"interruption-level": "time-sensitive"},
        }
        if event["kind"] == "step":
            data["actions"] = [
                _action(timer["id"], "extend", "+1 min"),
                _action(timer["id"], "pause", "Pause"),
            ]
        await self._send(self._targets(timer), timer["label"], message, data, f"{self._tag(timer)}_alert")

    async def _live(self, timer: dict) -> None:
        """Ongoing notification with a native countdown to the next step."""
        steps = timer["steps"]
        current = steps[timer["current_index"]]["label"] if timer["current_index"] >= 0 else "Starting"
        nxt = steps[timer["next_index"]]["label"] if timer["next_index"] is not None else "Finishing up"
        data: dict[str, Any] = {
            "tag": self._tag(timer),
            "channel": "html_artifacts_live",
            "importance": "low",
            "alert_once": True,
            "sticky": "true",
            "persistent": True,
        }
        if timer["state"] == "running" and timer.get("next_due"):
            message = f"Now: {current}\nNext: {nxt}"
            data["chronometer"] = True  # counts down to `when`
            data["when"] = int(timer["next_due"])
            data["actions"] = [
                _action(timer["id"], "next", "Next step"),
                _action(timer["id"], "extend", "+1 min"),
                _action(timer["id"], "pause", "Pause"),
            ]
        else:
            message = f"Paused at: {current}"
            data["actions"] = [_action(timer["id"], "resume", "Resume")]
        await self._send(self._targets(timer), timer["label"], message, data, f"{self._tag(timer)}_live")

    async def _clear(self, timer: dict) -> None:
        for target in self._targets(timer):
            try:
                await self._hass.services.async_call(
                    "notify",
                    target,
                    {"message": "clear_notification", "data": {"tag": self._tag(timer)}},
                    blocking=False,
                )
            except Exception:  # noqa: BLE001
                pass

    async def async_custom(self, artifact_id: str, title: str | None, message: str) -> None:
        """A one-off notification requested by an artifact page."""
        pseudo = {"id": f"custom_{artifact_id}", "artifact_id": artifact_id}
        data = {"tag": f"htmlart_custom_{artifact_id}", "channel": "html_artifacts_alerts"}
        await self._send(self._targets(pseudo), title or artifact_id, message, data, f"htmlart_custom_{artifact_id}")
