"""Phone notifications and bus events for server-side timers."""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import (
    ACTION_PREFIX,
    CONF_LIVE,
    CONF_NOTIFY_TARGETS,
    EVENT_FINISHED,
    EVENT_GATE,
    EVENT_OVERTIME,
    EVENT_STEP,
    PANEL_URL_PATH,
)
from .core import ArtifactStore

_LOGGER = logging.getLogger(__name__)

LIVE_CHANGES = {"start", "resume", "skip", "extend", "step", "pause", "gate", "answer", "overtime"}
CLEAR_CHANGES = {"reset", "delete", "finish"}


def _action(timer_id: str, verb: str, title: str, track: str | None = None, arg: str | None = None) -> dict[str, str]:
    """Notification button. Flat timers keep the original 3-part payload."""
    payload = f"{ACTION_PREFIX}|{timer_id}|{verb}"
    if track:
        payload += f"|{track}"
        if arg is not None:
            payload += f"|{arg}"
    return {"action": payload, "title": title}


def _mmss(seconds: float | None) -> str:
    total = max(0, int(round(seconds or 0)))
    return f"{total // 60}:{total % 60:02d}"


def _track_label(timer: dict, track: str | None) -> str | None:
    if not track or track == "*":
        return None
    return next((t["label"] for t in timer.get("tracks", []) if t["id"] == track), track)


class TimerNotifier:
    """Turns timer events into notifications on the user's phone."""

    def __init__(self, hass: HomeAssistant, artifacts: ArtifactStore, entry: ConfigEntry) -> None:
        self._hass = hass
        self._artifacts = artifacts
        self._entry = entry
        self._warned: set[str] = set()
        self._open_gates: dict[str, set[str]] = {}

    # -- helpers ---------------------------------------------------------

    def _targets(self, timer: dict) -> list[str]:
        raw = timer.get("notify_targets") or self._entry.options.get(CONF_NOTIFY_TARGETS) or []
        return [t.removeprefix("notify.") for t in raw]

    def _allowed(self, timer: dict) -> bool:
        return bool(self._artifacts.get_grants(timer.get("artifact_id", "")).get("notify"))

    @staticmethod
    def _tag(timer: dict) -> str:
        return f"htmlart_{timer['id']}"

    @staticmethod
    def _link(artifact_id: str | None) -> str | None:
        """Where tapping a notification goes: the panel, already showing this artifact."""
        return f"/{PANEL_URL_PATH}?artifact={quote(artifact_id, safe='')}" if artifact_id else None

    async def _send(
        self, targets: list[str], title: str, message: str, data: dict[str, Any], notification_id: str,
        artifact_id: str | None = None,
    ) -> None:
        link = self._link(artifact_id)
        if link:
            data = {**data, "clickAction": link, "url": link}  # Android / iOS
        if not targets:
            body = f"{message}\n\n[Open the page]({link})" if link else message
            await self._hass.services.async_call(
                "persistent_notification",
                "create",
                {"title": title, "message": body, "notification_id": notification_id},
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
            kind = event["kind"]
            data = {
                "timer_id": timer["id"],
                "artifact_id": timer["artifact_id"],
                "label": timer["label"],
                "step": (event.get("step") or {}).get("label"),
                "index": event.get("index"),
                "track": event.get("track"),
                "phase": event.get("phase", "start"),
                "reminder": kind == "reminder",
                "missed": event.get("missed", 0),
            }
            if kind == "overtime":
                data.update(seconds_over=event.get("over", 0), reminder=event.get("reminder", False))
            if kind == "gate":
                gate = (event.get("step") or {}).get("gate") or {}
                data.update(question=gate.get("question"), options=gate.get("more", []))
            if kind == "finished":
                data.update(
                    history=timer.get("history", []),
                    step_log=timer.get("step_log", []),
                    planned=timer.get("planned_duration"),
                    started=timer.get("started_wall"),
                    finished=timer.get("finished_wall"),
                )
            self._hass.bus.async_fire(
                {"step": EVENT_STEP, "reminder": EVENT_STEP, "gate": EVENT_GATE, "overtime": EVENT_OVERTIME,
                 "finished": EVENT_FINISHED}[kind], data
            )
            if self._allowed(timer):
                self._hass.async_create_task(self._alert(event))
        elif event["type"] == "timer":
            timer = event["timer"]
            change = event.get("change")
            if timer["id"] in self._open_gates:
                still = {w["track"] for w in timer.get("waiting", [])}
                self._hass.async_create_task(self._close_gates(timer, still))
            if change == "delete" or not self._allowed(timer) or not self._entry.options.get(CONF_LIVE, True):
                if change == "delete" and self._allowed_id(timer):
                    self._hass.async_create_task(self._clear(timer))
                return
            if change in LIVE_CHANGES:
                self._hass.async_create_task(self._live(timer))
            elif change in CLEAR_CHANGES:
                self._hass.async_create_task(self._clear(timer))

    def _allowed_id(self, timer: dict) -> bool:
        return self._allowed(timer)

    # -- the three kinds of notification ---------------------------------------------

    async def _alert(self, event: dict) -> None:
        timer, step = event["timer"], event.get("step")
        track = event.get("track")
        prefix = f"{_track_label(timer, track)} — " if track and _track_label(timer, track) else ""
        if event["kind"] == "gate":
            await self._gate_alert(event, prefix)
            return
        if event["kind"] == "overtime":
            await self._overtime_alert(event, prefix)
            return
        if event["kind"] == "reminder":
            # A nudge on its own tag: the ongoing notification keeps showing the current step.
            message = prefix + step["label"] + (f" - {step['detail']}" if step.get("detail") else "")
            data = {
                "tag": f"{self._tag(timer)}_reminder_{event['index']}",
                "channel": "html_artifacts_alerts",
                "importance": "high",
                "priority": "high",
                "ttl": 0,
                "push": {"interruption-level": "time-sensitive"},
            }
            await self._send(self._targets(timer), timer["label"], message, data, data["tag"], timer["artifact_id"])
            return
        if event["kind"] == "finished":
            message = (step or {}).get("label") or "Done"
            if step:
                message = f"Done: {message}"
        elif event.get("phase") == "end":
            message = prefix + (step.get("endLabel") or f"{step['label']} done")
        else:
            message = prefix + step["label"]
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
            actions = [_action(timer["id"], "extend", "+1 min", track)]
            if track:
                actions.append(_action(timer["id"], "next", "Next", track))
            actions.append(_action(timer["id"], "pause", "Pause", track))
            data["actions"] = actions
        await self._send(self._targets(timer), timer["label"], message, data, f"{self._tag(timer)}_alert", timer["artifact_id"])

    async def _overtime_alert(self, event: dict, prefix: str) -> None:
        timer, step, track = event["timer"], event["step"], event.get("track")
        if step.get("endLabel") and not event.get("reminder"):
            return  # the end-of-activity alert already said so
        message = prefix + (f"{step['label']}: +{_mmss(event.get('over'))} over" if event.get("reminder") else f"{step['label']}: time's up")
        data = {
            "tag": f"{self._tag(timer)}_alert",
            "channel": "html_artifacts_alerts",
            "importance": "high",
            "priority": "high",
            "ttl": 0,
            "push": {"interruption-level": "time-sensitive"},
            "actions": [_action(timer["id"], "next", "Next", track), _action(timer["id"], "extend", "+1 min", track)],
        }
        await self._send(self._targets(timer), timer["label"], message, data, f"{self._tag(timer)}_alert", timer["artifact_id"])

    async def _gate_alert(self, event: dict, prefix: str) -> None:
        timer, step, track, index = event["timer"], event["step"], event.get("track") or "main", event["index"]
        gate = step.get("gate") or {}
        tag = f"{self._tag(timer)}_gate_{track}"
        self._open_gates.setdefault(timer["id"], set()).add(track)
        actions = [_action(timer["id"], "answer", gate.get("yes", "Done"), track, f"{index}:yes")]
        for seconds in gate.get("more", [])[:2]:
            title = f"+{seconds // 60} min" if seconds % 60 == 0 else f"+{seconds} s"
            actions.append(_action(timer["id"], "answer", title, track, f"{index}:{seconds}"))
        data = {
            "tag": tag,
            "channel": "html_artifacts_alerts",
            "importance": "high",
            "priority": "high",
            "ttl": 0,
            "sticky": "true",
            "persistent": True,
            "push": {"interruption-level": "time-sensitive"},
            "actions": actions,
        }
        message = f"{prefix}{step['label']}: {gate.get('question', 'Done yet?')}"
        await self._send(self._targets(timer), timer["label"], message, data, tag, timer["artifact_id"])

    async def _close_gates(self, timer: dict, still_waiting: set[str]) -> None:
        open_tracks = self._open_gates.get(timer["id"], set())
        closing = open_tracks - still_waiting
        for name in closing:
            open_tracks.discard(name)
            await self._clear_tag(timer, f"{self._tag(timer)}_gate_{name}")
            if not self._targets(timer):
                try:
                    await self._hass.services.async_call(
                        "persistent_notification", "dismiss",
                        {"notification_id": f"{self._tag(timer)}_gate_{name}"}, blocking=False,
                    )
                except Exception:  # noqa: BLE001
                    pass
        if not open_tracks:
            self._open_gates.pop(timer["id"], None)

    def _focus(self, timer: dict) -> str | None:
        """The track the Next / +1 min buttons should act on."""
        if not timer.get("multi"):
            return None
        for lane in [*timer.get("tracks", []), timer.get("join")]:
            if lane and lane.get("next_index") is not None and lane["next_index"] == timer["next_index"] and lane.get("state") != "waiting":
                return lane["id"]
        for lane in timer.get("tracks", []):
            if lane.get("state") == "waiting":
                return lane["id"]
        return None

    def _lane_text(self, timer: dict) -> str:
        steps = timer["steps"]
        pieces = []
        for lane in timer.get("tracks", []):
            if lane["state"] in ("done", "idle", "finished"):
                continue
            cur = steps[lane["current_index"]]["label"] if lane["current_index"] >= 0 else "Starting"
            if lane["state"] == "waiting":
                pieces.append(f"{lane['label']}: {cur}?")
                continue
            if lane.get("overtime", 0) > 0:
                pieces.append(f"{lane['label']}: {cur} +{_mmss(lane['overtime'])} over")
                continue
            seconds = lane["remaining"] if lane.get("remaining") is not None else lane.get("next_in")
            pieces.append(f"{lane['label']}: {cur} {_mmss(seconds)}" if seconds is not None else f"{lane['label']}: {cur}")
        join = timer.get("join")
        if join and join["state"] == "running" and not pieces:
            pieces.append(steps[join["next_index"]]["label"] if join["next_index"] is not None else "Finishing up")
        return " · ".join(pieces) or "Finishing up"

    async def _live(self, timer: dict) -> None:
        """Ongoing notification with a native countdown to the next step."""
        steps = timer["steps"]
        multi = bool(timer.get("multi"))
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
        focus = self._focus(timer)
        waiting = bool(timer.get("waiting"))
        if timer["state"] == "running":
            over = timer.get("overtime", 0)
            if multi:
                message = self._lane_text(timer)
            elif over > 0:
                message = f"{current}  +{_mmss(over)} over\nNext: {nxt}"
            else:
                message = f"Now: {current}\nNext: {nxt}"
            if over > 0:
                data["chronometer"] = True  # `when` in the past: counts UP from the moment time ran out
                data["when"] = int(timer["server_now"] - over)
            elif timer.get("next_due") and not (waiting and timer["next_index"] is None):
                data["chronometer"] = True  # counts down to `when`
                data["when"] = int(timer["next_due"])
            actions = []
            if not (waiting and focus is None):
                actions.append(_action(timer["id"], "next", "Next step", focus))
                actions.append(_action(timer["id"], "extend", "+1 min", focus))
            actions.append(_action(timer["id"], "pause", "Pause"))
            data["actions"] = actions
        else:
            message = f"Paused: {self._lane_text(timer)}" if multi else f"Paused at: {current}"
            data["actions"] = [_action(timer["id"], "resume", "Resume")]
        await self._send(self._targets(timer), timer["label"], message, data, f"{self._tag(timer)}_live", timer["artifact_id"])

    async def _clear_tag(self, timer: dict, tag: str) -> None:
        for target in self._targets(timer):
            try:
                await self._hass.services.async_call(
                    "notify", target, {"message": "clear_notification", "data": {"tag": tag}}, blocking=False
                )
            except Exception:  # noqa: BLE001
                pass

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
        await self._send(self._targets(pseudo), title or artifact_id, message, data, f"htmlart_custom_{artifact_id}", artifact_id)
