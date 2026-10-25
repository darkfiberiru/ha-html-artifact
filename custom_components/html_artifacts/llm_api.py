"""LLM API ("HTML Artifacts") exposing artifact tools to Claude.

Home Assistant's MCP server integration can serve this API, so Claude Desktop,
Claude Code or any MCP client on the network can read, publish and drive
artifacts. The same code backs the REST API in http.py.
"""
from __future__ import annotations

import asyncio

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType

from .const import DOMAIN, LLM_API_ID
from .core import ArtifactError, lint_html

AUTHORING_GUIDE = """\
HTML ARTIFACTS FOR HOME ASSISTANT - AUTHORING GUIDE

An artifact is one self-contained HTML page (inline CSS and JS) that you publish
into Home Assistant. It is shown in a sidebar panel or dashboard card, in a
sandboxed iframe, and can be edited by hand. Any kind of page is fine: trackers,
checklists, dashboards, kitchen displays, planners, games. Unlike a normal
artifact, it can keep state on the server and use `window.ha` (below).

WORKFLOW
1. artifact_list (ids, versions, sizes, capabilities), then artifact_read before
   changing anything. artifact_read with metadata_only=true returns just the
   version, size and capabilities, without the html, when you only need the version.
2. New page: artifact_publish with the full html. Changing an existing page:
   prefer artifact_patch with find/replace edits (old_str must match exactly once;
   all edits apply or none do) and base_version set to the version you read: you
   resend only what changed. Use artifact_publish with the full html for rewrites.
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
- One file, no build step. External scripts only from cdnjs.cloudflare.com,
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
        out = {k: v for k, v in result.items() if k != "html"}
        out["lint"] = lint_html(args["html"], out.get("caps_requested"))
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
            caps_requested=args.get("capabilities"),
            source="claude",
            updated_by="claude",
        )
        out = {k: v for k, v in result.items() if k != "html"}
        out["lint"] = lint_html(result["html"], out.get("caps_requested"))
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
            "lint": lint_html(artifact["html"], artifact.get("caps_requested")),
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
        "done). Phone notifications need the artifact's 'notify' capability to be approved."
    )
    parameters = vol.Schema(
        {
            vol.Required("artifact_id"): str,
            vol.Required("label"): str,
            vol.Optional("steps"): [dict],
            vol.Optional("tracks"): [vol.Any(dict, str)],
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


class ArtifactsAPI(llm.API):
    """The LLM API registered with Home Assistant."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass=hass, id=LLM_API_ID, name="HTML Artifacts")

    async def async_get_api_instance(self, llm_context: llm.LLMContext) -> llm.APIInstance:
        tools: list[llm.Tool] = [
            GuideTool(), ListTool(), ReadTool(), PublishTool(), PatchTool(), CheckTool(), KvGetTool(), KvSetTool(),
            TimerListTool(), TimerCreateTool(), TimerControlTool(),
        ]
        return llm.APIInstance(api=self, api_prompt=SHORT_PROMPT, llm_context=llm_context, tools=tools)
