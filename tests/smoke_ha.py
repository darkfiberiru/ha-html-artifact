"""Smoke test against a real Home Assistant install (optional).

    python -m venv /tmp/hav && /tmp/hav/bin/pip install homeassistant
    cd <repo> && /tmp/hav/bin/python tests/smoke_ha.py

Drives the real websocket handlers, MCP/LLM tools, REST views and the
timer -> phone notification path with a real `HomeAssistant` object. It does not
start the HTTP server or the frontend, so panel registration is not covered.
"""
import asyncio
import json
import pathlib
import sys
import tempfile
import threading
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from homeassistant.components import websocket_api  # noqa: E402
from homeassistant.core import Context, HomeAssistant, ServiceCall  # noqa: E402
from homeassistant.helpers import frame, llm  # noqa: E402
from voluptuous_openapi import convert  # noqa: E402

from custom_components.html_artifacts import Runtime, make_scheduler  # noqa: E402
from custom_components.html_artifacts.const import CONF_LIVE, CONF_NOTIFY_TARGETS, DOMAIN, EVENT_FINISHED, EVENT_STEP  # noqa: E402
from custom_components.html_artifacts.core import ArtifactStore, TimerEngine  # noqa: E402
from custom_components.html_artifacts.http import ArtifactListView, ArtifactView  # noqa: E402
from custom_components.html_artifacts.llm_api import ArtifactsAPI  # noqa: E402
from custom_components.html_artifacts.notifier import TimerNotifier  # noqa: E402
from custom_components.html_artifacts.websocket_api import async_register_websocket  # noqa: E402
from homeassistant.helpers.storage import Store  # noqa: E402
import time  # noqa: E402

PASSED = []


def ok(name):
    PASSED.append(name)
    print("  ok ", name)


class Conn:
    def __init__(self, admin=True):
        self.user = SimpleNamespace(name="Alex", is_admin=admin, id="u1")
        self.messages = []
        self.subscriptions = {}

    def send_result(self, msg_id, result=None):
        self.messages.append({"id": msg_id, "success": True, "result": result})

    def send_error(self, msg_id, code, message, *args, **kwargs):
        self.messages.append({"id": msg_id, "success": False, "error": {"code": code, "message": message}})

    def send_message(self, msg):
        self.messages.append(msg)

    def context(self, msg):
        return Context()


async def main():
    hass = HomeAssistant(tempfile.mkdtemp())
    # A started Home Assistant sets these; without them its thread-safety checks
    # think every call comes from outside the event loop.
    hass.loop_thread_id = threading.get_ident()
    frame.async_setup(hass)

    # --- runtime wired like async_setup_entry does ---------------------------------
    entry = SimpleNamespace(options={CONF_NOTIFY_TARGETS: ["notify.mobile_app_test"], CONF_LIVE: True})
    artifacts = ArtifactStore(Store(hass, 1, f"{DOMAIN}.artifacts"))
    timers = TimerEngine(Store(hass, 1, f"{DOMAIN}.timers"), time.time, make_scheduler(hass))
    notifier = TimerNotifier(hass, artifacts, entry)
    timers.add_listener(notifier.handle)
    hass.data[DOMAIN] = {"runtime": Runtime(artifacts, timers, notifier)}

    sent = []

    async def notify_handler(call: ServiceCall):
        sent.append(dict(call.data))

    hass.services.async_register("notify", "mobile_app_test", notify_handler)
    bus_events = []
    hass.bus.async_listen(EVENT_STEP, lambda e: bus_events.append(("step", dict(e.data))))
    hass.bus.async_listen(EVENT_FINISHED, lambda e: bus_events.append(("finished", dict(e.data))))
    hass.bus.async_listen("html_artifacts_timer_gate", lambda e: bus_events.append(("gate", dict(e.data))))

    # --- websocket handlers -----------------------------------------------------------
    async_register_websocket(hass)
    handlers = hass.data[websocket_api.DOMAIN]
    conn = Conn()
    counter = [0]

    async def ws(cmd, **fields):
        counter[0] += 1
        msg = {"id": counter[0], "type": f"{DOMAIN}/{cmd}", **fields}
        handler, schema = handlers[msg["type"]]
        handler(hass, conn, schema(msg) if schema else msg)  # HA stores False when only "type" is declared
        await asyncio.sleep(0.05)
        reply = [m for m in conn.messages if m.get("id") == msg["id"]][-1]
        return reply

    r = await ws("save", artifact_id="pesto", html="<h1>v1</h1>", title="Pesto", caps_requested={"notify": True})
    assert r["success"] and r["result"]["version"] == 1, r
    ok("ws save creates an artifact")

    r = await ws("save", artifact_id="pesto", html="<h1>v2</h1>", base_version=1)
    assert r["result"]["version"] == 2
    r = await ws("save", artifact_id="pesto", html="<h1>stale</h1>", base_version=1)
    assert not r["success"] and r["error"]["code"] == "conflict" and r["error"]["current"]["html"] == "<h1>v2</h1>", r
    ok("ws save reports conflicts with the current content")

    r = await ws("kv/set", artifact_id="pesto", key="checked", value=[0, 2])
    assert r["success"]
    r = await ws("kv/get", artifact_id="pesto", key="checked")
    assert r["result"]["value"] == [0, 2]
    r = await ws("kv/list", artifact_id="pesto")
    assert r["result"]["values"] == {"checked": [0, 2]}
    ok("ws kv set/get/list")

    r = await ws("timers/create", artifact_id="ghost", label="x", steps=[], duration=5)
    assert not r["success"] and r["error"]["code"] == "not_found"
    ok("timers need an existing artifact")

    # subscription forwards events
    counter[0] += 1
    sub_id = counter[0]
    handler, schema = handlers[f"{DOMAIN}/subscribe"]
    handler(hass, conn, {"id": sub_id, "type": f"{DOMAIN}/subscribe"})
    assert sub_id in conn.subscriptions
    artifacts.kv_set("pesto", "live", 1)
    forwarded = [m for m in conn.messages if m.get("type") == "event" and m.get("id") == sub_id]
    assert forwarded and forwarded[-1]["event"]["key"] == "live", conn.messages[-2:]
    conn.subscriptions[sub_id]()
    ok("ws subscribe forwards store events, and unsubscribes")

    # --- admin gating -------------------------------------------------------------------
    plain = Conn(admin=False)
    handler, schema = handlers[f"{DOMAIN}/set_grants"]
    try:
        handler(hass, plain, schema({"id": 99, "type": f"{DOMAIN}/set_grants", "artifact_id": "pesto", "grants": {}}))
    except Exception as err:  # Unauthorized
        assert type(err).__name__ == "Unauthorized", err
    else:
        raise AssertionError("non-admin was allowed to set grants")
    ok("set_grants is admin-only")

    # --- notifications are gated by the grant -------------------------------------------
    steps = [{"at": 0, "label": "Start"}, {"at": 0.5, "label": "Flip", "detail": "carefully"}]
    r = await ws("timers/create", artifact_id="pesto", label="Pesto Tilapia", steps=steps, duration=1.2, start=True)
    assert r["success"], r
    await asyncio.sleep(1.7)
    assert sent == [], f"no notification expected before the grant, got {sent}"
    assert [k for k, _ in bus_events] == ["step", "finished"], bus_events
    ok("no phone notifications without the grant (bus events still fire)")

    r = await ws("set_grants", artifact_id="pesto", grants={"notify": True})
    assert r["success"] and r["result"]["caps_granted"]["notify"] is True
    sent.clear()
    bus_events.clear()
    r = await ws("timers/create", artifact_id="pesto", label="Pesto Tilapia", steps=steps, duration=1.2, start=True)
    tid = r["result"]["timer"]["id"]
    await asyncio.sleep(1.7)
    messages = [s["message"] for s in sent]
    live = [s for s in sent if s["data"].get("chronometer")]
    alerts = [s for s in sent if s["data"].get("tag", "").endswith("_alert")]
    assert live and isinstance(live[0]["data"]["when"], int), sent
    assert any(a["message"].startswith("Flip - carefully") for a in alerts), messages
    assert any(m.startswith("Done") or m == "Done" for m in messages), messages
    assert any(s["message"] == "clear_notification" for s in sent), messages
    assert any(a["data"].get("actions") for a in alerts)
    assert all(s["title"] == "Pesto Tilapia" for s in sent if s["message"] != "clear_notification")
    ok("granted: live countdown, step alert with actions, finish alert, and clear all reach the phone")
    r = await ws("timers/list", artifact_id="pesto")
    assert [t["state"] for t in r["result"]["timers"]].count("finished") == 2
    ok("finished timers persist for any device to read")

    # --- tracks and gates on the phone -----------------------------------------------------
    from custom_components.html_artifacts import handle_notification_action  # noqa: E402

    sent.clear()
    bus_events.clear()
    lane_steps = [
        {"track": "rice", "at": 0, "label": "Start the rice"},
        {"track": "rice", "at": 0.4, "label": "Rice ready"},
        {"track": "fish", "at": 1.0, "label": "Check the fish", "gate": {"question": "145F?", "more": [60, 120]}},
        {"track": "*", "label": "Serve"},
    ]
    r = await ws("timers/create", artifact_id="pesto", label="Tikka", steps=lane_steps,
                 tracks=[{"id": "rice", "label": "Rice"}, {"id": "fish", "label": "Fish"}], start=True)
    assert r["success"], r
    lane_id = r["result"]["timer"]["id"]
    await asyncio.sleep(1.5)
    alerts = [s for s in sent if s["data"].get("tag", "").endswith("_alert")]
    assert any(a["message"] == "Rice — Rice ready" for a in alerts), [a["message"] for a in alerts]
    gate_msgs = [s for s in sent if "_gate_" in s["data"].get("tag", "") and s["message"] != "clear_notification"]
    assert gate_msgs and "145F?" in gate_msgs[0]["message"], sent
    titles = [a["title"] for a in gate_msgs[0]["data"]["actions"]]
    assert titles == ["Done", "+1 min", "+2 min"], titles
    live = [s for s in sent if s["data"].get("tag") == f"htmlart_{lane_id}" and s["message"] != "clear_notification"]
    assert live and "Fish: Check the fish?" in live[-1]["message"], [s["message"] for s in live]
    assert ("gate", "fish") in [(k, d.get("track")) for k, d in bus_events], bus_events
    assert any(k == "step" and d.get("track") == "rice" for k, d in bus_events), bus_events
    ok("tracks: lane-named alerts, one live notification with every lane, gate question with Done / +1 / +2 buttons")

    sent.clear()
    index = gate_msgs[0]["data"]["actions"][0]["action"].split("|")[4].split(":")[0]
    assert handle_notification_action(timers, gate_msgs[0]["data"]["actions"][1]["action"].replace("|60", "|60"))
    await asyncio.sleep(0.05)
    view = timers.get_view(lane_id)
    assert view["tracks"][1]["state"] == "running" and view["tracks"][1]["offset"] == 60, view["tracks"][1]
    assert any(s["message"] == "clear_notification" and "_gate_" in s["data"]["tag"] for s in sent), sent
    ok("tapping +1 min on the gate extends that lane and clears the question")
    timers.control(lane_id, "delete")

    # timers cannot be driven through another artifact
    await ws("save", artifact_id="other", html="x")
    r = await ws("timers/control", artifact_id="other", timer_id=tid, action="reset")
    assert not r["success"] and r["error"]["code"] == "not_found"
    ok("a page cannot control another artifact's timers")

    # --- MCP / LLM API ------------------------------------------------------------------
    api = ArtifactsAPI(hass)
    ctx = llm.LLMContext(platform="test", context=Context(), language="en", assistant=None, device_id=None)
    inst = await api.async_get_api_instance(ctx)
    names = sorted(t.name for t in inst.tools)
    assert "artifact_publish" in names and "artifact_authoring_guide" in names, names
    for tool in inst.tools:
        schema = convert(tool.parameters)  # what the MCP server does for tools/list
        assert schema.get("type") == "object", (tool.name, schema)
    ok(f"{len(names)} LLM tools convert to JSON schema for MCP")

    async def tool(name, **args):
        return await inst.async_call_tool(llm.ToolInput(tool_name=name, tool_args=args))

    guide = await tool("artifact_authoring_guide")
    assert "window.ha API" in guide["guide"]
    res = await tool("artifact_publish", artifact_id="cook", html="<p>one</p>", title="Cook", capabilities={"notify": True, "entities": ["sensor.oven_*"]})
    assert res["version"] == 1 and res["caps_requested"]["entities"] == ["sensor.oven_*"], res
    res = await tool("artifact_publish", artifact_id="cook", html="<p>blind overwrite</p>")
    assert res["error"] == "conflict" and res["current"]["html"] == "<p>one</p>", res
    r = await ws("save", artifact_id="cook", html="<p>one, edited by hand</p>", base_version=1)
    assert r["success"]
    res = await tool("artifact_publish", artifact_id="cook", html="<p>stale</p>", base_version=1)
    assert res["error"] == "conflict" and res["current"]["updated_by"] == "user:Alex", res
    res = await tool("artifact_read", artifact_id="cook")
    res = await tool("artifact_publish", artifact_id="cook", html=res["html"] + "<p>merged</p>", base_version=res["version"])
    assert res["version"] == 3 and res["updated_by"] == "claude"
    ok("Claude's publish flow: read, conflict on stale version, merge, publish")

    # --- patch / metadata / render check --------------------------------------------------
    assert {"artifact_patch", "artifact_check"} <= set(names), names
    meta = await tool("artifact_read", artifact_id="cook", metadata_only=True)
    assert "html" not in meta and meta["version"] == 3 and meta["size"] > 0 and "caps_requested" in meta, meta
    listed = (await tool("artifact_list"))["artifacts"]
    assert all("size" in a and "version" in a for a in listed)
    ok("artifact_read(metadata_only) returns version, size and capabilities without html")

    res = await tool("artifact_patch", artifact_id="cook", base_version=3,
                     edits=[{"old_str": "<p>merged</p>", "new_str": "<p>patched</p><script>localStorage.x=1</script>"}])
    assert res["version"] == 4 and "html" not in res, res
    assert any(w["code"] == "storage" for w in res["lint"]), res["lint"]
    full = await tool("artifact_read", artifact_id="cook")
    assert "<p>patched</p>" in full["html"] and "<p>one, edited by hand</p>" in full["html"]
    res = await tool("artifact_patch", artifact_id="cook", base_version=4, edits=[{"old_str": "<p>", "new_str": "<q>"}])
    assert res["error"] == "edit_failed" and res["matches"] >= 2, res
    res = await tool("artifact_patch", artifact_id="cook", base_version=1, edits=[{"old_str": "patched", "new_str": "x"}])
    assert res["error"] == "conflict" and res["current"]["version"] == 4, res
    assert (await tool("artifact_read", artifact_id="cook", metadata_only=True))["version"] == 4, "failed patches change nothing"
    ok("artifact_patch: applies edits, lints, rejects ambiguous edits and stale versions")

    res = await tool("artifact_check", artifact_id="cook", wait_seconds=0)
    assert res["version"] == 4 and res["render"]["status"] == "pending" and "hint" in res["render"], res
    assert any(w["code"] == "storage" for w in res["lint"])

    # a browser reports while artifact_check is waiting for it
    async def browser_reports():
        await asyncio.sleep(0.3)
        r = await ws("report", artifact_id="cook", version=4, source="dry-run", settled=True,
                     errors=[{"level": "error", "message": "ReferenceError: x is not defined", "line": 7}])
        assert r["success"] and r["result"] == {"stale": False}, r

    waiter = asyncio.create_task(tool("artifact_check", artifact_id="cook", wait_seconds=5))
    await browser_reports()
    res = await asyncio.wait_for(waiter, 5)
    assert res["render"]["status"] == "errors" and res["render"]["errors"][0]["line"] == 7, res
    assert res["render"]["reported_by"] == ["dry-run"]
    r = await ws("report", artifact_id="cook", version=3, source="viewer", settled=True)
    assert r["result"]["stale"] is True, r
    r = await ws("report", artifact_id="nope", version=1, source="viewer", settled=True)
    assert not r["success"] and r["error"]["code"] == "not_found", r
    res = await tool("artifact_patch", artifact_id="cook", base_version=4, edits=[{"old_str": "<script>localStorage.x=1</script>", "new_str": ""}])
    res = await tool("artifact_check", artifact_id="cook", wait_seconds=0)
    assert res["version"] == 5 and res["render"]["status"] == "pending", "a new version starts without the old report"
    ok("artifact_check waits for a browser report, reports errors with lines, ignores stale versions")

    res = await tool("artifact_kv_set", artifact_id="cook", key="notes", value={"a": [1, 2]})
    res = await tool("artifact_kv_get", artifact_id="cook")
    assert res["values"] == {"notes": {"a": [1, 2]}}
    res = await tool("artifact_timer_create", artifact_id="cook", label="Rice", duration=600, steps=[{"at": 0, "label": "go"}], start=True)
    t = res["timer"]
    assert t["state"] == "running"
    res = await tool("artifact_timer_control", timer_id=t["id"], action="pause")
    assert res["timer"]["state"] == "paused"
    res = await tool("artifact_timer_list", artifact_id="cook")
    assert len(res["timers"]) == 1
    ok("LLM kv and timer tools")

    await tool("artifact_kv_set", artifact_id="cook", key="checked-pesto-1", value=True)
    await tool("artifact_kv_set", artifact_id="cook", key="checked-tikka-1", value=True)
    res = await tool("artifact_kv_get", artifact_id="cook", prefix="checked-pesto-")
    assert list(res["values"]) == ["checked-pesto-1"], res
    res = await tool("artifact_read", artifact_id="cook", include_html=False)
    assert "html" not in res and res["version"] == 5, res
    res = await tool("artifact_patch", artifact_id="cook", base_version=5, edits=[{"old": "<p>patched</p>", "new": "<p class=x>patched</p>"}])
    assert res["version"] == 6 and "html" not in res, res
    res = await tool(
        "artifact_timer_create", artifact_id="cook", label="Tikka", start=True,
        tracks=[{"id": "rice", "label": "Rice"}, {"id": "fish", "label": "Fish"}],
        steps=[{"track": "rice", "at": 0, "label": "Start", "duration": 600},
               {"track": "fish", "at": 300, "label": "Check", "gate": {"question": "Done?"}},
               {"track": "*", "label": "Serve"}],
    )
    assert [t["id"] for t in res["timer"]["tracks"]] == ["rice", "fish"], res
    res = await tool("artifact_timer_control", timer_id=res["timer"]["id"], action="extend", seconds=60, track="fish")
    assert res["timer"]["tracks"][1]["offset"] == 60, res["timer"]["tracks"]
    ok("MCP: kv prefix, include_html=false, old/new patch aliases, timers with tracks and per-lane control")

    # --- REST views ----------------------------------------------------------------------
    class Req(dict):
        def __init__(self, body=None, admin=True, query=None):
            super().__init__(hass_user=SimpleNamespace(name="Script", is_admin=admin))
            self._body = body
            self.query = query or {}

        async def json(self):
            return self._body

    view = ArtifactView(hass)
    listing = ArtifactListView(hass)
    resp = await view.put(Req({"html": "<b>rest</b>", "title": "Rest"}), "rest-page")
    assert resp.status == 200
    resp = await view.put(Req({"html": "<b>again</b>"}), "rest-page")
    assert resp.status == 409
    resp = await view.put(Req({"html": "<b>again</b>", "base_version": 1}), "rest-page")
    assert resp.status == 200
    resp = await view.get(Req(), "rest-page")
    assert resp.status == 200 and b"again" in resp.body
    resp = await view.get(Req(query={"metadata": "1"}), "rest-page")
    meta = json.loads(resp.body)
    assert resp.status == 200 and "html" not in meta and meta["version"] == 2 and meta["size"] > 0, meta
    resp = await view.patch(Req({"base_version": 2, "edits": [{"old_str": "again", "new_str": "patched"}]}), "rest-page")
    body = json.loads(resp.body)
    assert resp.status == 200 and body["version"] == 3 and "html" not in body, body
    resp = await view.patch(Req({"base_version": 3, "edits": [{"old_str": "nothere", "new_str": "x"}]}), "rest-page")
    assert resp.status == 422 and json.loads(resp.body)["error"] == "edit_failed"
    resp = await view.patch(Req({"base_version": 1, "edits": [{"old_str": "patched", "new_str": "x"}]}), "rest-page")
    assert resp.status == 409
    resp = await view.patch(Req({"edits": [{"old_str": "patched", "new_str": "x"}]}), "rest-page")
    assert resp.status == 409
    resp = await view.patch(Req({"base_version": 3}), "rest-page")
    assert resp.status == 400
    resp = await view.get(Req(), "rest-page")
    assert b"patched" in resp.body
    resp = await listing.get(Req())
    assert resp.status == 200
    resp = await view.get(Req(), "missing")
    assert resp.status == 404
    resp = await view.delete(Req(admin=False), "rest-page")
    assert resp.status == 403
    resp = await view.delete(Req(admin=True), "rest-page")
    assert resp.status == 200
    ok("REST: create, 409 on missing base_version, metadata read, PATCH edits (200/409/422/400), list, 404, admin-only delete")

    # delete cascades to timers
    await ws("delete", artifact_id="cook")
    assert timers.list("cook") == []
    ok("deleting an artifact deletes its timers")

    timers.async_shutdown()
    await hass.async_stop(force=True)
    print(f"\n{len(PASSED)} smoke checks passed against real Home Assistant")


asyncio.run(main())
