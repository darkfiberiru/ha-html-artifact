"""LLM API ("HTML Artifacts") exposing artifact tools to Claude.

Home Assistant's MCP server integration can serve this API, so Claude Desktop,
Claude Code or any MCP client on the network can read, publish and drive
artifacts. The same code backs the REST API in http.py.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import functools

import aiohttp
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util.json import JsonObjectType

from .const import DOMAIN, LLM_API_ID
from .assets import MAX_TOTAL_BYTES
from .core import ArtifactError, lint_html

AUTHORING_GUIDE = """\
HTML ARTIFACTS FOR HOME ASSISTANT - AUTHORING GUIDE

An artifact is one self-contained HTML page (inline CSS and JS) that you publish
into Home Assistant. It is shown in a sidebar panel or dashboard card, in a
sandboxed iframe, and can be edited by hand. Home Assistant is the host: it stores
the page and its data (kv, assets), runs timers, signs the user in and sends
notifications. It is shared by everyone with HA access and has no public link, so
do not design a page to be opened outside HA or rely on anything but `window.ha`
for state shared between devices. Any kind of page is fine: trackers,
checklists, dashboards, kitchen displays, planners, games. Unlike a normal
artifact, it can keep state on the server and use `window.ha` (below).

DISPLAY TYPE: DASHBOARD CARD OR PAGE (decide first, pass it to artifact_publish)
  display "card"  - sits on a dashboard next to other cards (what's cooking, a chore tile).
                    Compact, readable at a glance, about 350-500 px wide, NO inner scrolling,
                    use HA theme variables (--primary-text-color, --card-background-color ...),
                    and respect card_height (pixels, or "auto" to size to the content).
  display "page"  - a full-screen app opened from the Artifacts sidebar or a link (Recipes,
                    a cook-along). Full viewport, mobile-first, sticky controls are fine.
                    Never shrunk into a card. This is the default for new artifacts.
  display "both"  - adapts. Read `ha.host` ("card" | "panel" | "full"; ha.hostHeight is the
                    card height in px or null) after `await ha.ready`, and subscribe with
                    ha.onHostChange(cb). `<html data-ha-host="card">` is set for CSS.
  Artifacts that existed before this setting count as "both" until you set it. To change
  only display / card_height, call artifact_publish with the same html and base_version:
  no new version is created.
  artifact_check renders a card at 420 px wide at its card_height, and a page at phone and
  desktop width; lint warns when a card is taller than its card_height.

WORKFLOW
1. artifact_list (ids, versions, sizes, capabilities), then artifact_read before
   changing anything. artifact_read with metadata_only=true returns just the
   version, size and capabilities, without the html, when you only need the version.
2. New page: artifact_publish with the full html. Changing an existing page:
   prefer artifact_patch with find/replace edits (old_str must match exactly once;
   all edits apply or none do) and base_version set to the version you read: you
   resend only what changed. Use artifact_publish with the full html for rewrites.
   A page too big to send comfortably (say over 50 KB) never needs to pass through
   the conversation: upload it as an asset with artifact_assets_upload_url
   (path "page.html"), then artifact_publish with html_asset="page.html".
   If the user edited the page in the meantime you get error "conflict" with the
   current html: re-read, merge their changes into yours and try again. Never
   overwrite blindly.
3. Check your work: publish and patch return `lint` warnings (things the sandbox
   will block, such as localStorage or external scripts). Then call artifact_check:
   a real browser renders the new version in a hidden, side-effect-free frame
   (no service calls, notifications or timers) and reports JavaScript errors,
   console errors and blocked requests with line numbers. If it says "pending",
   no browser had Home Assistant open: ask the user to open it, or retry later.
   Fix what it reports with artifact_patch, then check again.
4. Declare what the page needs in `capabilities`; the user approves it in the
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
- localStorage, sessionStorage, indexedDB and cookies throw in the sandbox; use ha.kv.
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
  Parallel lanes (tracks): ha.timers.create({label, tracks: [{id:"rice", label:"Rice"},
      {id:"fish", label:"Fish"}], steps: [{track:"rice", at, label}, ...]}). Each lane has
      its own offset, so extending the fish never moves the rice. Steps without `track`
      share one implicit lane (old pages are unaffected). A join step -
      {track:"*", label:"Serve"} or {after:["rice","fish"], label} - fires once every
      listed lane is finished (omit `at`; it sorts last). duration defaults to the longest lane.
  Step activities: {at, label, duration: 960, endLabel?: "Simmer done"}. While inside it the
      countdown is the time LEFT in the activity (view.current.remaining; the phone shows
      "Simmer 9:40"), and endLabel alerts when it ends.
  Check gates ("cook until done"): {track, at, label, gate: {question: "145F?", yes?: "Done",
      more?: [60,120], autoYesAfter?: 600}}. The lane pauses and the step stays current
      (view.current.waiting) until ha.timers.answer(id, stepIndex, "yes" | seconds). The phone
      shows the question with Done / +1 min / +2 min; +N pushes the lane back and asks again.
  Manual advance ("cook until I say"): create({advance:"manual", steps:[...]}) or per step
      {advance:"manual"|"auto", remindEvery?: seconds}.
        manual: when its time is up the timer does NOT move on: it stays on that step and counts
                overtime (current.remaining goes negative, view.overtime > 0: show "+1:23 over").
                Next (skip) ends it and every later step shifts to when Next was pressed
                (early Next pulls them sooner); extend() in overtime gives it more time first.
        auto:   becomes current and moves on by itself when the next step is due (the default).
        reminder: {reminder:true} (or kind:"reminder"): alerts on time, NEVER becomes current,
                never ends the current step; Next skips past it. anchor:"step" (default) = offset
                from the start of the step it sits under: moves with overtime/early Next, and is
                marked skipped (not fired) if Next ends that step first. anchor:"timer" = fixed
                offset from timer start, unaffected by overtime. A reminder can't have advance,
                gate, duration, endLabel or after.
        A manual step that is last in its lane and has a duration holds from its planned end until
        Next (no closing marker step needed). Next on a step followed by a gate lands on the gate.
      view.reminders = [{index, label, state: pending|fired|skipped, due (seconds, null once
      done or before its step starts), armed, firedAt}] - show "Rice check - 2 min ago" chips.
      timer.step_log = [{index, track, label, planned, actual}] for every Next.
      timer.extra = [{index, track, label, gate, extend, seconds}]: time added to a step by gate
      answers (gate) and by extend/+1 min (extend), including on auto steps. reset() and
      start() restore the schedule as created. Only the step's own lane holds.
      Bus: html_artifacts_timer_overtime {step, seconds_over, reminder}; reminders fire
      html_artifacts_timer_step with reminder:true. The phone's ongoing notification keeps the
      current manual step; a reminder gets its own alert.
  ha.timers.fromDurations([{label, seconds, detail?, track?}])  (sync) -> {steps, duration}
      (emits `duration` on each step, so the countdown shows time left in each step)
  ha.timers.list(), start(id), pause(id[, {track}]), resume(id[, {track}]), reset(id),
      skip(id[, toIndex | {track}]), extend(id, seconds[, {track}]), answer(id, stepIndex, yesOrSeconds),
      remove(id).  Without {track}, pause/skip/extend act on every lane (skip: the lane
      whose step is next).
  ha.timers.subscribe(cb)                   cb(timersForThisArtifact) now and on change
  ha.timers.view(t) (sync)                  adds live fields from a timer t:
      elapsed, remaining, current, currentIndex, next, nextIndex, nextIn (the soonest event
      across lanes) and tracks: [{id, label, state, current, next, nextIn, remaining, offset,
      waiting, gate}] where state is running|paused|waiting|done. current.remaining and
      current.waiting are set when the step is an activity / an unanswered gate. waiting lists
      unanswered gates.
      Call it every ~250ms from a render loop to animate a countdown; it uses the
      time since the timer was received, so device clocks do not matter.
  ha.timers.onFired(cb)                     cb({kind: "step"|"gate"|"finished", phase, track, timer, step})
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
  "models/base.stl". Allowed types: images, audio/video, json/txt/csv/md/css/js, html,
  fonts, pdf, stl/obj/glb/gltf/3mf/bin. Limits: 8 MB per file, 64 MB and 1000
  files per artifact. Publish the page first; assets belong to an existing artifact.
  Uploading, cheapest first:
  0. artifact_assets_upload_url: a single-use, short-lived URL for one file (path)
     or one zip (zip=true). If you can run a command where the file is, push it
     yourself: curl -sS -X PUT --data-binary @file "<url>". No token needed, and
     the bytes never pass through the conversation.
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
Events on the HA bus: html_artifacts_timer_step (has track and phase start|end),
html_artifacts_timer_gate (question, options), html_artifacts_timer_finished (has history,
planned, started, finished) - usable in automations.
timer.history lists every start/pause/resume/extend/skip/answer with time, track, step index
and source (page|notification|mcp|auto); finished timers are kept for 7 days, so a page that
was closed during the cook can still record how the run went (planned_duration vs actual).
Phone: one ongoing notification per timer lists every active lane ("Fish: Simmer 9:40 ·
Rice: Steaming 3:10"); alerts are named ("Rice — Rice ready"); +1 min / Next / Pause act on
the lane that just fired. Use artifact_timer_create to start such a timer from chat.
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


# llm.ToolResult does not exist on the older cores hacs.json still claims
# support for, so resolve it once and fall back to the plain-dict contract
# there rather than raising at import time.
_ToolResult = getattr(llm, "ToolResult", None)


def _guard(func):
    """Turn ArtifactError into a tool error, and return HA's ToolResult.

    Both concerns live here so every tool body stays a plain function that
    returns a dict:

    * Returning a bare dict from a tool is deprecated and stops working in HA
      2027.11 -- llm.APIInstance.async_call_tool wraps it and logs the usage.
      Wrapping it ourselves keeps the log clean and the behaviour identical.
    * An ArtifactError is an expected outcome (conflict, bad input), not a
      crash, so it comes back as a result flagged error=True.
    """

    @functools.wraps(func)
    async def wrapper(self, hass, tool_input, llm_context):
        try:
            result = await func(self, hass, tool_input, llm_context)
        except ArtifactError as err:
            payload = {"error": err.code, "message": err.message, **err.extra}
            return _ToolResult(data=payload, error=True) if _ToolResult else payload
        if _ToolResult is None or isinstance(result, _ToolResult):
            return result
        return _ToolResult(data=result)

    return wrapper


class _Tool(llm.Tool):
    # Set once for every tool in this module. Without it,
    # llm.APIInstance.__post_init__ reports each tool as "provided without an
    # integration", which becomes an error in HA 2027.10. Harmless on cores
    # that predate the attribute.
    integration = DOMAIN


class GuideTool(_Tool):
    name = "artifact_authoring_guide"
    description = "Read this first: how to write HTML artifacts and the window.ha API they can use."
    parameters = vol.Schema({})

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        return {"guide": AUTHORING_GUIDE}


class ListTool(_Tool):
    name = "artifact_list"
    description = "List published artifacts (id, title, version, size in bytes, requested and granted capabilities)."
    parameters = vol.Schema({})

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        return {"artifacts": _rt(hass).artifacts.list()}


class ReadTool(_Tool):
    name = "artifact_read"
    description = (
        "Read an artifact's html and version. Always do this before changing a page: the "
        "user may have edited it by hand. With include_html=false (or metadata_only=true) you get "
        "only the version, size, title and capabilities (no html), which is enough to pass as "
        "base_version."
    )
    parameters = vol.Schema(
        {
            vol.Required("artifact_id"): str,
            vol.Optional("metadata_only"): bool,
            vol.Optional("include_html"): bool,
        }
    )

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        args = tool_input.tool_args
        artifact = _rt(hass).artifacts.get(args["artifact_id"])
        if args.get("metadata_only") or args.get("include_html") is False:
            artifact.pop("html", None)
        return artifact


class PublishTool(_Tool):
    name = "artifact_publish"
    description = (
        "Create or update an artifact with a complete self-contained HTML document. "
        "For an existing artifact pass base_version from artifact_read; a stale version "
        "returns a 'conflict' error with the current html to merge. 'capabilities' "
        "declares what the page needs: {entities: [patterns], services: [domain.service "
        "patterns], notify: bool, network: bool}; the user must approve them. "
        "Instead of 'html' you can pass 'html_asset': the path of an .html asset file of this "
        "artifact (upload it first with artifact_assets_upload_url), so a large page never "
        "passes through the conversation; the asset is removed after publishing unless keep_asset=true."
    )
    parameters = vol.Schema(
        {
            vol.Required("artifact_id"): str,
            vol.Optional("html"): str,
            vol.Optional("html_asset"): str,
            vol.Optional("keep_asset"): bool,
            vol.Optional("base_version"): int,
            vol.Optional("title"): str,
            vol.Optional("description"): str,
            vol.Optional("icon"): str,
            vol.Optional("display"): vol.In(["card", "page", "both"]),
            vol.Optional("card_height"): vol.Any(int, str),
            vol.Optional("capabilities"): dict,
        }
    )

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        args = tool_input.tool_args
        rt = _rt(hass)
        asset_path = args.get("html_asset")
        if ("html" in args) == bool(asset_path):
            raise ArtifactError("invalid", "pass exactly one of html or html_asset")
        if asset_path:
            if not asset_path.lower().endswith(".html"):
                raise ArtifactError("invalid", "html_asset must be an .html asset file")
            data, _meta = await rt.async_assets(rt.assets.read, args["artifact_id"], asset_path)
            try:
                html = data.decode("utf-8")
            except UnicodeDecodeError as err:
                raise ArtifactError("invalid", f"html_asset is not UTF-8 text: {err}") from err
        else:
            html = args["html"]
        result = rt.artifacts.save(
            args["artifact_id"],
            html,
            title=args.get("title"),
            description=args.get("description"),
            icon=args.get("icon"),
            display=args.get("display"),
            card_height=args.get("card_height"),
            base_version=args.get("base_version"),
            caps_requested=args.get("capabilities"),
            source="claude",
            updated_by="claude",
        )
        out = {k: v for k, v in result.items() if k != "html"}
        if asset_path:
            out["from_asset"] = asset_path
            if not args.get("keep_asset"):
                await rt.async_assets(rt.assets.delete, args["artifact_id"], asset_path)
                out["asset_removed"] = True
        out["lint"] = lint_html(html, out.get("caps_requested"), out.get("display"), out.get("card_height"))
        out["next"] = "Call artifact_check to see how it renders in a real browser."
        return out


class PatchTool(_Tool):
    name = "artifact_patch"
    description = (
        "Change an existing artifact with find/replace edits instead of resending the whole page. "
        "edits = [{old_str, new_str}] (old/new also work) applied in order; each old_str must appear EXACTLY once in "
        "the page (add surrounding text to make it unique); all edits apply or none do. "
        "base_version is required: the version you read (artifact_read, metadata_only is fine). "
        "A stale version returns a 'conflict' with the current html. Returns the new version, "
        "lint warnings and no html."
    )
    parameters = vol.Schema(
        {
            vol.Required("artifact_id"): str,
            vol.Required("base_version"): int,
            vol.Required("edits"): [
                vol.Schema(
                    {
                        vol.Optional("old_str"): str,
                        vol.Optional("new_str"): str,
                        vol.Optional("old"): str,
                        vol.Optional("new"): str,
                    }
                )
            ],
            vol.Optional("title"): str,
            vol.Optional("description"): str,
            vol.Optional("icon"): str,
            vol.Optional("display"): vol.In(["card", "page", "both"]),
            vol.Optional("card_height"): vol.Any(int, str),
            vol.Optional("capabilities"): dict,
        }
    )

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        args = tool_input.tool_args
        store = _rt(hass).artifacts
        edits = []
        for edit in args["edits"]:
            old = edit.get("old_str", edit.get("old"))
            new = edit.get("new_str", edit.get("new"))
            if old is None or new is None:
                raise ArtifactError("invalid", "each edit needs old_str and new_str (or old and new)")
            edits.append({"old_str": old, "new_str": new})
        result = store.patch(
            args["artifact_id"],
            edits,
            base_version=args["base_version"],
            title=args.get("title"),
            description=args.get("description"),
            icon=args.get("icon"),
            display=args.get("display"),
            card_height=args.get("card_height"),
            caps_requested=args.get("capabilities"),
            source="claude",
            updated_by="claude",
        )
        out = {k: v for k, v in result.items() if k != "html"}
        out["lint"] = lint_html(result["html"], out.get("caps_requested"), out.get("display"), out.get("card_height"))
        out["next"] = "Call artifact_check to see how it renders in a real browser."
        return out


class CheckTool(_Tool):
    name = "artifact_check"
    description = (
        "Render check for the CURRENT version of an artifact. Returns static lint plus what a "
        "real browser saw when it ran the page in a hidden, side-effect-free frame: JavaScript "
        "errors, unhandled promise rejections, console.error output and requests blocked by the "
        "security policy, with line numbers in your html. Waits up to wait_seconds (default 20) "
        "for a browser to report. status: ok | warnings | errors | pending. 'pending' means no "
        "browser has Home Assistant open: ask the user to open it (the check then runs on its own) "
        "and call again. Page logic that depends on live data is stubbed, so data-dependent "
        "behavior still needs a human look."
    )
    parameters = vol.Schema({vol.Required("artifact_id"): str, vol.Optional("wait_seconds"): vol.Coerce(float)})

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        args = tool_input.tool_args
        rt = _rt(hass)
        artifact_id = args["artifact_id"]
        artifact = rt.artifacts.get(artifact_id)
        version = artifact["version"]
        wait = min(max(float(args.get("wait_seconds", 20)), 0.0), 60.0)

        def settled() -> bool:
            report = rt.reports.summary(artifact_id, version)
            return bool(report and report["settled"])

        if not settled() and wait:
            done = asyncio.Event()

            def on_report(event: dict) -> None:
                if event.get("artifact_id") == artifact_id and settled():
                    done.set()

            remove = rt.reports.add_listener(on_report)
            try:
                await asyncio.wait_for(done.wait(), wait)
            except asyncio.TimeoutError:
                pass
            finally:
                remove()

        report = rt.reports.summary(artifact_id, version)
        errors = report["errors"] if report else []
        if any(e["level"] in ("error", "console") for e in errors):
            status = "errors"
        elif errors:
            status = "warnings"
        elif report and report["settled"]:
            status = "ok"
        else:
            status = "pending"
        render: dict = {"status": status, "errors": errors, "renders": report["renders"] if report else 0}
        lint = lint_html(artifact["html"], artifact.get("caps_requested"), artifact.get("display"), artifact.get("card_height"))
        if report and report.get("frames"):
            render["frames"] = report["frames"]
            height = artifact.get("card_height")
            if artifact.get("display") in ("card", "both") and isinstance(height, int):
                for frame in report["frames"]:
                    if frame["w"] <= 600 and frame["h"] == height and frame["content"] > height + 24:
                        lint.append({"level": "warning", "code": "card_overflow", "message":
                                     f"as a {frame['w']}px-wide card the page is {frame['content']}px tall but card_height "
                                     f"is {height}: it will scroll inside the card. Raise card_height, use \"auto\", or tighten the layout."})
        if report:
            render["reported_by"] = report["sources"]
        if status == "pending":
            render["hint"] = (
                "No browser reported on this version yet. Home Assistant must be open in a browser "
                "or the companion app (any page) for the background check to run; opening the "
                "Artifacts panel also works."
            )
        return {
            "artifact_id": artifact_id,
            "version": version,
            "lint": lint,
            "render": render,
        }


class KvGetTool(_Tool):
    name = "artifact_kv_get"
    description = (
        "Read an artifact's shared state: one key, or all keys if key is omitted. "
        "prefix limits the 'all keys' result to keys starting with it (e.g. 'checked-pesto-')."
    )
    parameters = vol.Schema(
        {vol.Required("artifact_id"): str, vol.Optional("key"): str, vol.Optional("prefix"): str}
    )

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        args = tool_input.tool_args
        store = _rt(hass).artifacts
        if "key" in args:
            return {"value": store.kv_get(args["artifact_id"], args["key"])}
        values = store.kv_all(args["artifact_id"])
        if args.get("prefix"):
            values = {k: v for k, v in values.items() if k.startswith(args["prefix"])}
        return {"values": values}


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
        "label, detail?, notify?, track?, duration?, endLabel?, gate?, after?}], duration = total "
        "seconds (default: the longest lane). tracks = [{id, label}] makes parallel lanes "
        "(rice / fish): give steps a track id; track '*' (or after:[ids]) is a join step that "
        "fires when those lanes are finished. A step duration makes it an activity "
        "('Simmer, 9:40 left'; endLabel announces its end). gate = {question, yes?, more?: "
        "[60,120], autoYesAfter?} pauses that lane until the phone/page answers (cook until "
        "done). advance='manual' (timer default, or per step) makes a step WAIT for Next when its time is up: it counts overtime (negative "
        "remaining) and later steps shift to when Next is pressed; remind_every re-alerts while over. "
        "Steps with reminder:true (anchor 'step' or 'timer') only alert on time: they never become "
        "current and Next skips past them. "
        "Phone notifications need the artifact's 'notify' capability to be approved."
    )
    parameters = vol.Schema(
        {
            vol.Required("artifact_id"): str,
            vol.Required("label"): str,
            vol.Optional("steps"): [dict],
            vol.Optional("tracks"): [vol.Any(dict, str)],
            vol.Optional("advance"): vol.In(["auto", "manual"]),
            vol.Optional("remind_every"): vol.Coerce(float),
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
            None, bool(args.get("start")), tracks=args.get("tracks"),
            advance=args.get("advance"), remind_every=args.get("remind_every"),
        )
        return {"timer": timer}


class TimerControlTool(_Tool):
    name = "artifact_timer_control"
    description = (
        "Control a timer: start, pause, resume, reset, skip, extend (seconds), answer or delete. "
        "track limits pause/resume/skip/extend to one lane. answer needs step_index (the gate "
        "step) and answer = 'yes' or extra seconds."
    )
    parameters = vol.Schema(
        {
            vol.Required("timer_id"): str,
            vol.Required("action"): vol.In(
                ["start", "pause", "resume", "reset", "skip", "extend", "answer", "delete"]
            ),
            vol.Optional("seconds"): vol.Coerce(float),
            vol.Optional("track"): str,
            vol.Optional("step_index"): int,
            vol.Optional("answer"): vol.Any(str, int, float, bool),
        }
    )

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        args = tool_input.tool_args
        timer = _rt(hass).timers.control(
            args["timer_id"], args["action"], seconds=args.get("seconds"), track=args.get("track"),
            to_index=args.get("step_index"), answer=args.get("answer"), source="mcp",
        )
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


class AssetsUploadUrlTool(_Tool):
    name = "artifact_assets_upload_url"
    description = (
        "Get a single-use, short-lived URL to upload one asset file (give 'path') or one zip of "
        "assets (zip=true, optional prefix/replace) with a plain HTTP PUT, e.g. "
        "curl -sS -X PUT --data-binary @file \"<url>\". The bytes go straight to Home Assistant, "
        "not through the conversation. Optional sha256 is checked on arrival."
    )
    parameters = vol.Schema(
        {
            vol.Required("artifact_id"): str,
            vol.Optional("path"): str,
            vol.Optional("zip"): bool,
            vol.Optional("prefix"): str,
            vol.Optional("replace"): bool,
            vol.Optional("sha256"): str,
            vol.Optional("expires_minutes"): vol.Coerce(float),
        }
    )

    @_guard
    async def async_call(self, hass, tool_input, llm_context) -> JsonObjectType:
        args = tool_input.tool_args
        rt = _rt(hass)
        aid = args["artifact_id"]
        _need_artifact(rt, aid)
        zipped = bool(args.get("zip"))
        if not zipped and not args.get("path"):
            raise ArtifactError("invalid", "give 'path' for a single file, or zip=true for a zip")
        ticket = rt.uploads.issue(
            aid,
            mode="zip" if zipped else "file",
            path=args.get("path"),
            prefix=args.get("prefix", ""),
            replace=bool(args.get("replace")),
            sha256=args.get("sha256"),
            ttl=float(args.get("expires_minutes", 15)) * 60,
        )
        path = f"/api/html_artifacts/upload/{ticket['token']}"
        urls = []
        for kwargs in ({"allow_external": False}, {"allow_internal": False}):
            try:
                base = get_url(hass, **kwargs)
            except NoURLAvailableError:
                continue
            if base + path not in urls:
                urls.append(base + path)
        return {
            "method": "PUT",
            "path": path,
            "urls": urls,
            "expires_at": ticket["expires_at"],
            "max_bytes": ticket["max_bytes"],
            "single_use": True,
            "target": ticket["path"] if ticket["mode"] == "file" else "zip import",
            "example": f'curl -sS -X PUT --data-binary @<file> "{(urls or [path])[0]}"',
        }


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
            GuideTool(), ListTool(), ReadTool(), PublishTool(), PatchTool(), CheckTool(), KvGetTool(), KvSetTool(),
            TimerListTool(), TimerCreateTool(), TimerControlTool(),
            AssetsListTool(), AssetsUploadUrlTool(), AssetPutTool(), AssetsImportTool(), AssetDeleteTool(),
        ]
        return llm.APIInstance(api=self, api_prompt=SHORT_PROMPT, llm_context=llm_context, tools=tools)
