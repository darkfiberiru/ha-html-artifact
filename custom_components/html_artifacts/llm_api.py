"""LLM API ("HTML Artifacts") exposing artifact tools to Claude.

Home Assistant's MCP server integration can serve this API, so Claude Desktop,
Claude Code or any MCP client on the network can read, publish and drive
artifacts. The same code backs the REST API in http.py.
"""
from __future__ import annotations

import base64
import binascii

import aiohttp
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.json import JsonObjectType

from .const import DOMAIN, LLM_API_ID
from .assets import MAX_TOTAL_BYTES
from .core import ArtifactError

AUTHORING_GUIDE = """\
HTML ARTIFACTS FOR HOME ASSISTANT - AUTHORING GUIDE

An artifact is one self-contained HTML page (inline CSS and JS) that you publish
into Home Assistant. It is shown in a sidebar panel or dashboard card, in a
sandboxed iframe, and can be edited by hand. Any kind of page is fine: trackers,
checklists, dashboards, kitchen displays, planners, games. Unlike a normal
artifact, it can keep state on the server and use `window.ha` (below).

WORKFLOW
1. artifact_list, then artifact_read before changing anything. The result has a
   `version`.
2. artifact_publish with the full html and `base_version` set to the version you
   read. New artifacts need no base_version. If the user edited the page in the
   meantime you get error "conflict" with the current html: merge their changes
   into yours and publish again. Never overwrite blindly.
3. Declare what the page needs in `capabilities`; the user approves it in the
   panel. Until then the page runs without those powers:
   {"entities": ["sensor.oven_*"], "services": ["light.turn_on"],
    "notify": true, "network": false}
   (patterns allow * and ?; "network": true allows external images and fetch)

PAGE RULES
- One file (max 2 MB), no build step. Put anything bulky (images, 3D models,
  data files) in ASSET FILES below instead of data: URIs. External scripts only from cdnjs.cloudflare.com,
  cdn.jsdelivr.net or unpkg.com; fonts from Google Fonts.
- Works on phones: viewport meta, 16px gutters, large tap targets.
- Respect the theme: the root element gets data-theme="light|dark", and the CSS
  variables --primary-color, --primary-text-color, --card-background-color,
  --primary-background-color, --secondary-text-color, --divider-color are set.
- Do not use localStorage for anything that matters; use ha.kv.
- Wait for `await ha.ready` before calling other ha.* functions.

window.ha API (all methods return promises unless noted)
  ha.user, ha.artifact, ha.grants           set after ready
  ha.kv.get(key) / set(key, value) / delete(key) / list()
  ha.kv.subscribe(cb)                       cb({key, value, deleted}) on ANY device's change
      Shared, persistent JSON state. Use it for checked items, counters, notes,
      form values - it is the same on every phone and survives restarts.
  ha.timers.create({label, steps, duration, start})
      Server-side timer: keeps running with the page closed, identical on every
      device, survives HA restarts. steps = [{at, label, detail?, notify?}] where
      `at` is seconds since start; duration is the total (defaults to last step).
      A step at 0 is shown but not announced. With the `notify` capability granted
      the user's phone gets an alert per step, a live countdown notification and
      Pause / +1 min / Next buttons.
  ha.timers.fromDurations([{label, seconds, detail?}])  (sync) -> {steps, duration}
  ha.timers.list(), start(id), pause(id), resume(id), reset(id),
      skip(id[, toIndex]), extend(id, seconds), remove(id)
  ha.timers.subscribe(cb)                   cb(timersForThisArtifact) now and on change
  ha.timers.view(t) (sync)                  adds live fields from a timer t:
      elapsed, remaining, current, currentIndex, next, nextIndex, nextIn
      Call it every ~250ms from a render loop to animate a countdown; it uses the
      time since the timer was received, so device clocks do not matter.
  ha.timers.onFired(cb)                     cb({kind: "step"|"finished", timer, step})
  ha.entities.get(entityIdOrArray)          needs `entities` grant
  ha.entities.subscribe(ids, cb)            cb(state, entityId) now and on change
  ha.callService(domain, service, data, target)   needs `services` grant
  ha.notify(message, title)                 needs `notify` grant
  ha.assets.list()                          -> {files: [{path, size, sha256, content_type}], ...}
  ha.assets.get(path[, as])                 as = "arraybuffer" (default) | "text" | "json" | "blob"
  await ha.assets.url(path)                 -> a blob: URL for <img>, <video>, CSS, fonts
      The page's own asset files (no grant needed). They are fetched through the
      panel, so they work with the default no-network policy.

ASSET FILES (per artifact, on the HA server, not versioned)
  Paths: letters, digits, . _ - and "/" folders, e.g. "img/plan.webp",
  "models/base.stl". Allowed types: images, audio/video, json/txt/csv/md/css/js,
  fonts, pdf, stl/obj/glb/gltf/3mf/bin. Limits: 8 MB per file, 64 MB and 1000
  files per artifact. Publish the page first; assets belong to an existing artifact.
  Uploading, cheapest first:
  1. artifact_assets_import with source "inbox:<name>.zip": a zip already placed in
     <config>/html_artifacts/inbox/ (e.g. copied there over a shared folder). The
     data never passes through the conversation.
  2. artifact_assets_import with an http(s) URL of a zip that HA can reach (LAN ok).
  3. artifact_asset_put with base64 data, in chunks of up to ~3 MB decoded: offset
     0 first, then offset = bytes received so far, final=true on the last chunk
     (optionally with sha256 to verify). Every byte goes through your context, so
     use it for small files.
  artifact_assets_list shows size and sha256 of every file; artifact_asset_delete
  removes one ("*" removes all).

Timer states: idle, running, paused, finished.
Events on the HA bus: html_artifacts_timer_step, html_artifacts_timer_finished
(usable in automations).
"""

SHORT_PROMPT = (
    "You can publish self-contained HTML pages into Home Assistant that stay "
    "editable by the user and can store shared state, run server-side timers, "
    "read entities and send phone notifications. Call artifact_authoring_guide "
    "before writing one, and always artifact_read before changing an existing page."
)


def _rt(hass: HomeAssistant):
    rt = hass.data.get(DOMAIN, {}).get("runtime")
    if rt is None:
        raise ArtifactError("not_loaded", "html_artifacts is not set up")
    return rt


def _guard(func):
    async def wrapper(self, hass, tool_input, llm_context) -> JsonObjectType:
        try:
            return await func(self, hass, tool_input, llm_context)
        except ArtifactError as err:
            return {"error": err.code, "message": err.message, **err.extra}

    return wrapper


class _Tool(llm.Tool):
    pass


class GuideTool(_Tool):
    name = "artifact_authoring_guide"
    description = "Read this first: how to write HTML artifacts and the window.ha API they can use."
    parameters = vol.Schema({})

    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        return {"guide": AUTHORING_GUIDE}


class ListTool(_Tool):
    name = "artifact_list"
    description = "List published artifacts (id, title, version, requested and granted capabilities)."
    parameters = vol.Schema({})

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        return {"artifacts": _rt(hass).artifacts.list()}


class ReadTool(_Tool):
    name = "artifact_read"
    description = (
        "Read an artifact's full html and version. Always do this before publishing a "
        "change: the user may have edited the page by hand."
    )
    parameters = vol.Schema({vol.Required("artifact_id"): str})

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        return _rt(hass).artifacts.get(tool_input.tool_args["artifact_id"])


class PublishTool(_Tool):
    name = "artifact_publish"
    description = (
        "Create or update an artifact with a complete self-contained HTML document. "
        "For an existing artifact pass base_version from artifact_read; a stale version "
        "returns a 'conflict' error with the current html to merge. 'capabilities' "
        "declares what the page needs: {entities: [patterns], services: [domain.service "
        "patterns], notify: bool, network: bool}; the user must approve them."
    )
    parameters = vol.Schema(
        {
            vol.Required("artifact_id"): str,
            vol.Required("html"): str,
            vol.Optional("base_version"): int,
            vol.Optional("title"): str,
            vol.Optional("description"): str,
            vol.Optional("icon"): str,
            vol.Optional("capabilities"): dict,
        }
    )

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        args = tool_input.tool_args
        result = _rt(hass).artifacts.save(
            args["artifact_id"],
            args["html"],
            title=args.get("title"),
            description=args.get("description"),
            icon=args.get("icon"),
            base_version=args.get("base_version"),
            caps_requested=args.get("capabilities"),
            source="claude",
            updated_by="claude",
        )
        return {k: v for k, v in result.items() if k != "html"}


class KvGetTool(_Tool):
    name = "artifact_kv_get"
    description = "Read an artifact's shared state (one key, or all keys if key is omitted)."
    parameters = vol.Schema({vol.Required("artifact_id"): str, vol.Optional("key"): str})

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        args = tool_input.tool_args
        store = _rt(hass).artifacts
        if "key" in args:
            return {"value": store.kv_get(args["artifact_id"], args["key"])}
        return {"values": store.kv_all(args["artifact_id"])}


class KvSetTool(_Tool):
    name = "artifact_kv_set"
    description = "Write a JSON value into an artifact's shared state; open pages update live."
    parameters = vol.Schema(
        {vol.Required("artifact_id"): str, vol.Required("key"): str, vol.Required("value"): object}
    )

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        args = tool_input.tool_args
        _rt(hass).artifacts.kv_set(args["artifact_id"], args["key"], args["value"])
        return {"ok": True}


class TimerListTool(_Tool):
    name = "artifact_timer_list"
    description = "List server-side timers (optionally for one artifact) with their live state."
    parameters = vol.Schema({vol.Optional("artifact_id"): str})

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        return {"timers": _rt(hass).timers.list(tool_input.tool_args.get("artifact_id"))}


class TimerCreateTool(_Tool):
    name = "artifact_timer_create"
    description = (
        "Create a server-side timer for an artifact. steps = [{at: seconds since start, "
        "label, detail?, notify?}], duration = total seconds. Phone notifications need "
        "the artifact's 'notify' capability to be approved."
    )
    parameters = vol.Schema(
        {
            vol.Required("artifact_id"): str,
            vol.Required("label"): str,
            vol.Optional("steps"): [dict],
            vol.Optional("duration"): vol.Coerce(float),
            vol.Optional("start"): bool,
        }
    )

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        args = tool_input.tool_args
        rt = _rt(hass)
        if not rt.artifacts.exists(args["artifact_id"]):
            raise ArtifactError("not_found", f"artifact '{args['artifact_id']}' does not exist")
        timer = rt.timers.create(
            args["artifact_id"], args["label"], args.get("steps"), args.get("duration"),
            None, bool(args.get("start")),
        )
        return {"timer": timer}


class TimerControlTool(_Tool):
    name = "artifact_timer_control"
    description = "Control a timer: start, pause, resume, reset, skip, extend (seconds) or delete."
    parameters = vol.Schema(
        {
            vol.Required("timer_id"): str,
            vol.Required("action"): vol.In(["start", "pause", "resume", "reset", "skip", "extend", "delete"]),
            vol.Optional("seconds"): vol.Coerce(float),
        }
    )

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        args = tool_input.tool_args
        timer = _rt(hass).timers.control(args["timer_id"], args["action"], seconds=args.get("seconds"))
        return {"timer": timer}


def _need_artifact(rt, artifact_id: str) -> None:
    if not rt.artifacts.exists(artifact_id):
        raise ArtifactError("not_found", f"artifact '{artifact_id}' does not exist: publish the page first")


class AssetsListTool(_Tool):
    name = "artifact_assets_list"
    description = "List an artifact's asset files (path, size, sha256, content_type) and the limits."
    parameters = vol.Schema({vol.Required("artifact_id"): str})

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        rt = _rt(hass)
        aid = tool_input.tool_args["artifact_id"]
        _need_artifact(rt, aid)
        return await rt.async_assets(rt.assets.list, aid)


class AssetPutTool(_Tool):
    name = "artifact_asset_put"
    description = (
        "Upload one asset file as base64, whole or in chunks (offset 0 first, then offset = "
        "bytes received so far; final=true on the last chunk moves it into place; sha256 "
        "optional). Every byte passes through the conversation: prefer artifact_assets_import "
        "for anything big."
    )
    parameters = vol.Schema(
        {
            vol.Required("artifact_id"): str,
            vol.Required("path"): str,
            vol.Required("data_base64"): str,
            vol.Optional("offset"): vol.Coerce(int),
            vol.Optional("final"): bool,
            vol.Optional("sha256"): str,
        }
    )

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        args = tool_input.tool_args
        rt = _rt(hass)
        _need_artifact(rt, args["artifact_id"])
        try:
            data = base64.b64decode(args["data_base64"], validate=True)
        except (binascii.Error, ValueError) as err:
            raise ArtifactError("invalid", f"data_base64 is not valid base64: {err}") from err
        return await rt.async_assets(
            rt.assets.put,
            args["artifact_id"],
            args["path"],
            data,
            offset=args.get("offset", 0),
            final=args.get("final", True),
            sha256=args.get("sha256"),
        )


class AssetsImportTool(_Tool):
    name = "artifact_assets_import"
    description = (
        "Import asset files from a zip without sending them through the conversation. "
        "source = 'inbox:<name>.zip' for a zip in <config>/html_artifacts/inbox/ (removed "
        "after a successful import unless keep=true), or an http(s) URL of a zip that Home "
        "Assistant can reach. prefix puts the files under a folder; replace=true deletes the "
        "artifact's existing assets first."
    )
    parameters = vol.Schema(
        {
            vol.Required("artifact_id"): str,
            vol.Required("source"): str,
            vol.Optional("prefix"): str,
            vol.Optional("replace"): bool,
            vol.Optional("keep"): bool,
        }
    )

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        args = tool_input.tool_args
        rt = _rt(hass)
        aid = args["artifact_id"]
        _need_artifact(rt, aid)
        source = args["source"].strip()
        inbox_file = None
        if source.startswith(("http://", "https://")):
            data = await _download(hass, source)
        elif source.startswith("inbox:"):
            inbox_file = await rt.async_assets(rt.assets.inbox_file, source[len("inbox:"):])
            data = await hass.async_add_executor_job(inbox_file.read_bytes)
        else:
            raise ArtifactError("invalid", "source must be 'inbox:<name>.zip' or an http(s) URL")
        result = await rt.async_assets(
            rt.assets.import_zip, aid, data, prefix=args.get("prefix", ""), replace=bool(args.get("replace"))
        )
        if inbox_file is not None and not args.get("keep"):
            await hass.async_add_executor_job(inbox_file.unlink)
            result["removed_from_inbox"] = str(inbox_file)
        return result


class AssetDeleteTool(_Tool):
    name = "artifact_asset_delete"
    description = "Delete one asset file of an artifact, or all of them with path '*'."
    parameters = vol.Schema({vol.Required("artifact_id"): str, vol.Required("path"): str})

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        args = tool_input.tool_args
        rt = _rt(hass)
        _need_artifact(rt, args["artifact_id"])
        path = None if args["path"].strip() == "*" else args["path"]
        removed = await rt.async_assets(rt.assets.delete, args["artifact_id"], path)
        return {"removed": removed}


async def _download(hass: HomeAssistant, url: str) -> bytes:
    """GET a zip, refusing anything bigger than the per-artifact asset limit."""
    session = async_get_clientsession(hass)
    limit = MAX_TOTAL_BYTES * 2
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status >= 400:
                raise ArtifactError("invalid", f"{url} returned HTTP {resp.status}")
            if resp.content_length and resp.content_length > limit:
                raise ArtifactError("too_large", f"download is larger than {limit} bytes")
            chunks, size = [], 0
            async for chunk in resp.content.iter_chunked(1 << 16):
                size += len(chunk)
                if size > limit:
                    raise ArtifactError("too_large", f"download is larger than {limit} bytes")
                chunks.append(chunk)
            return b"".join(chunks)
    except ArtifactError:
        raise
    except Exception as err:  # noqa: BLE001 - surface network errors to the caller
        raise ArtifactError("invalid", f"could not download {url}: {err}") from err


class ArtifactsAPI(llm.API):
    """The LLM API registered with Home Assistant."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass=hass, id=LLM_API_ID, name="HTML Artifacts")

    async def async_get_api_instance(self, llm_context: llm.LLMContext) -> llm.APIInstance:
        tools: list[llm.Tool] = [
            GuideTool(), ListTool(), ReadTool(), PublishTool(), KvGetTool(), KvSetTool(),
            TimerListTool(), TimerCreateTool(), TimerControlTool(),
            AssetsListTool(), AssetPutTool(), AssetsImportTool(), AssetDeleteTool(),
        ]
        return llm.APIInstance(api=self, api_prompt=SHORT_PROMPT, llm_context=llm_context, tools=tools)
