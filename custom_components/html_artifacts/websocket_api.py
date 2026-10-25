"""Websocket commands used by the panel, the card and the sandbox bridge.

The browser side is the trust boundary for entity reads and service calls
(it checks the artifact's grants before touching `hass`). Everything stored on
the server (artifacts, key/value state, timers, notifications) is enforced here.
"""
from __future__ import annotations

import inspect
import time
from typing import Any, Callable

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .core import ArtifactError


def _runtime(hass: HomeAssistant):
    return hass.data.get(DOMAIN, {}).get("runtime")


def _error(connection, msg_id: int, err: ArtifactError) -> None:
    connection.send_message(
        {
            "id": msg_id,
            "type": "result",
            "success": False,
            "error": {"code": err.code, "message": err.message, **err.extra},
        }
    )


def api(command: str, fields: dict | None = None, admin: bool = False) -> Callable:
    """Build a websocket command around `func(rt, connection, msg) -> dict`."""
    schema = {vol.Required("type"): f"{DOMAIN}/{command}", **(fields or {})}

    def decorator(func: Callable) -> Callable:
        @websocket_api.websocket_command(schema)
        @websocket_api.async_response
        async def handler(hass: HomeAssistant, connection, msg: dict) -> None:
            rt = _runtime(hass)
            if rt is None:
                connection.send_error(msg["id"], "not_loaded", "html_artifacts is not set up")
                return
            try:
                result = func(rt, connection, msg)
                if inspect.isawaitable(result):
                    result = await result
            except ArtifactError as err:
                _error(connection, msg["id"], err)
                return
            connection.send_result(msg["id"], result)

        return websocket_api.require_admin(handler) if admin else handler

    return decorator


AID = {vol.Required("artifact_id"): str}


@api("list")
def ws_list(rt, connection, msg):
    return {"artifacts": rt.artifacts.list()}


@api("get", AID)
def ws_get(rt, connection, msg):
    return rt.artifacts.get(msg["artifact_id"])


@api(
    "save",
    {
        **AID,
        vol.Required("html"): str,
        vol.Optional("title"): vol.Any(str, None),
        vol.Optional("description"): vol.Any(str, None),
        vol.Optional("icon"): vol.Any(str, None),
        vol.Optional("base_version"): vol.Any(int, None),
        vol.Optional("caps_requested"): vol.Any(dict, None),
    },
)
def ws_save(rt, connection, msg):
    return rt.artifacts.save(
        msg["artifact_id"],
        msg["html"],
        title=msg.get("title"),
        description=msg.get("description"),
        icon=msg.get("icon"),
        base_version=msg.get("base_version"),
        caps_requested=msg.get("caps_requested"),
        source="user",
        updated_by=f"user:{connection.user.name}",
    )


@api("delete", AID, admin=True)
def ws_delete(rt, connection, msg):
    rt.delete_artifact(msg["artifact_id"])
    return {}


@api("history", AID)
def ws_history(rt, connection, msg):
    return {"history": rt.artifacts.history(msg["artifact_id"])}


@api("revert", {**AID, vol.Required("version"): int})
def ws_revert(rt, connection, msg):
    return rt.artifacts.revert(msg["artifact_id"], msg["version"], f"user:{connection.user.name}")


@api("set_grants", {**AID, vol.Required("grants"): dict}, admin=True)
def ws_set_grants(rt, connection, msg):
    return rt.artifacts.set_grants(msg["artifact_id"], msg["grants"])


# -- key/value state ---------------------------------------------------------

@api("kv/get", {**AID, vol.Required("key"): str})
def ws_kv_get(rt, connection, msg):
    return {"value": rt.artifacts.kv_get(msg["artifact_id"], msg["key"])}


@api("kv/set", {**AID, vol.Required("key"): str, vol.Required("value"): object})
def ws_kv_set(rt, connection, msg):
    rt.artifacts.kv_set(msg["artifact_id"], msg["key"], msg["value"])
    return {}


@api("kv/delete", {**AID, vol.Required("key"): str})
def ws_kv_delete(rt, connection, msg):
    rt.artifacts.kv_delete(msg["artifact_id"], msg["key"])
    return {}


@api("kv/list", AID)
def ws_kv_list(rt, connection, msg):
    return {"values": rt.artifacts.kv_all(msg["artifact_id"])}


# -- asset files ---------------------------------------------------------------------

@api("assets/list", AID)
async def ws_assets_list(rt, connection, msg):
    if not rt.artifacts.exists(msg["artifact_id"]):
        raise ArtifactError("not_found", f"artifact '{msg['artifact_id']}' does not exist")
    return await rt.async_assets(rt.assets.list, msg["artifact_id"])


@api("assets/delete", {**AID, vol.Required("path"): str}, admin=True)
async def ws_assets_delete(rt, connection, msg):
    if not rt.artifacts.exists(msg["artifact_id"]):
        raise ArtifactError("not_found", f"artifact '{msg['artifact_id']}' does not exist")
    await rt.async_assets(rt.assets.delete, msg["artifact_id"], msg["path"])
    return {}


# -- timers ------------------------------------------------------------------------

@api("timers/list", {vol.Optional("artifact_id"): str})
def ws_timers_list(rt, connection, msg):
    return {"timers": rt.timers.list(msg.get("artifact_id")), "server_now": time.time()}


@api(
    "timers/create",
    {
        **AID,
        vol.Required("label"): str,
        vol.Optional("steps"): vol.Any([dict], None),
        vol.Optional("duration"): vol.Any(int, float, None),
        vol.Optional("start"): bool,
        vol.Optional("notify_targets"): vol.Any([str], None),
        vol.Optional("tracks"): vol.Any([vol.Any(dict, str)], None),
    },
)
def ws_timers_create(rt, connection, msg):
    if not rt.artifacts.exists(msg["artifact_id"]):
        raise ArtifactError("not_found", f"artifact '{msg['artifact_id']}' does not exist")
    timer = rt.timers.create(
        msg["artifact_id"],
        msg["label"],
        msg.get("steps"),
        msg.get("duration"),
        msg.get("notify_targets"),
        bool(msg.get("start")),
        tracks=msg.get("tracks"),
    )
    return {"timer": timer}


@api(
    "timers/control",
    {
        **AID,
        vol.Required("timer_id"): str,
        vol.Required("action"): vol.In(["start", "pause", "resume", "reset", "skip", "extend", "answer", "delete"]),
        vol.Optional("to_index"): vol.Any(int, None),
        vol.Optional("seconds"): vol.Any(int, float, None),
        vol.Optional("track"): vol.Any(str, None),
        vol.Optional("answer"): vol.Any(str, int, float, bool, None),
    },
)
def ws_timers_control(rt, connection, msg):
    timer = rt.timers.control(
        msg["timer_id"],
        msg["action"],
        artifact_id=msg["artifact_id"],
        to_index=msg.get("to_index"),
        seconds=msg.get("seconds"),
        track=msg.get("track"),
        answer=msg.get("answer"),
        source="page",
    )
    return {"timer": timer}


# -- render reports ----------------------------------------------------------------

@api(
    "report",
    {
        **AID,
        vol.Required("version"): int,
        vol.Required("source"): vol.In(["dry-run", "viewer"]),
        vol.Optional("settled", default=False): bool,
        vol.Optional("errors", default=[]): [dict],
    },
)
def ws_report(rt, connection, msg):
    """A browser tells us what happened when it ran an artifact (used by artifact_check)."""
    current = rt.artifacts.version_of(msg["artifact_id"])
    if current is None:
        raise ArtifactError("not_found", f"artifact '{msg['artifact_id']}' does not exist")
    if msg["version"] != current:
        return {"stale": True, "version": current}  # a newer version was published meanwhile
    rt.reports.add(
        msg["artifact_id"],
        current,
        source=msg["source"],
        settled=msg["settled"],
        errors=msg["errors"][:20],
    )
    return {"stale": False}


# -- notifications -----------------------------------------------------------------

@api(
    "notify",
    {**AID, vol.Required("message"): str, vol.Optional("title"): vol.Any(str, None)},
)
async def ws_notify(rt, connection, msg):
    if not rt.artifacts.get_grants(msg["artifact_id"]).get("notify"):
        raise ArtifactError("forbidden", "this artifact has not been granted notifications")
    await rt.notifier.async_custom(msg["artifact_id"], msg.get("title"), msg["message"][:500])
    return {}


# -- live updates ---------------------------------------------------------------------

@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection, msg: dict) -> None:
    rt = _runtime(hass)
    if rt is None:
        connection.send_error(msg["id"], "not_loaded", "html_artifacts is not set up")
        return

    @callback
    def forward(event: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], event))

    removers = [rt.artifacts.add_listener(forward), rt.timers.add_listener(forward)]

    @callback
    def unsubscribe() -> None:
        for remove in removers:
            remove()

    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    for handler in (
        ws_list, ws_get, ws_save, ws_delete, ws_history, ws_revert, ws_set_grants,
        ws_kv_get, ws_kv_set, ws_kv_delete, ws_kv_list,
        ws_assets_list, ws_assets_delete,
        ws_timers_list, ws_timers_create, ws_timers_control,
        ws_report, ws_notify, ws_subscribe,
    ):
        websocket_api.async_register_command(hass, handler)
