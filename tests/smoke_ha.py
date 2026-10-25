"""Smoke test against a real Home Assistant install (optional).

    python -m venv /tmp/hav && /tmp/hav/bin/pip install homeassistant
    cd <repo> && /tmp/hav/bin/python tests/smoke_ha.py

Drives the real websocket handlers, MCP/LLM tools, REST views and the
timer -> phone notification path with a real `HomeAssistant` object. It does not
start the HTTP server or the frontend, so panel registration is not covered.
"""
import asyncio
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
from custom_components.html_artifacts.assets import AssetStore  # noqa: E402
from custom_components.html_artifacts.core import ArtifactStore, TimerEngine  # noqa: E402
from custom_components.html_artifacts.http import ArtifactListView, ArtifactView, AssetListView, AssetView  # noqa: E402
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
    assets = AssetStore(hass.config.path(DOMAIN))
    hass.data[DOMAIN] = {"runtime": Runtime(artifacts, timers, notifier, assets, hass)}

    sent = []

    async def notify_handler(call: ServiceCall):
        sent.append(dict(call.data))

    hass.services.async_register("notify", "mobile_app_test", notify_handler)
    bus_events = []
    hass.bus.async_listen(EVENT_STEP, lambda e: bus_events.append(("step", dict(e.data))))
    hass.bus.async_listen(EVENT_FINISHED, lambda e: bus_events.append(("finished", dict(e.data))))

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

    # --- asset files over MCP ------------------------------------------------------------
    import base64, hashlib, io, os, zipfile  # noqa: E401

    res = await tool("artifact_assets_list", artifact_id="ghost")
    assert res["error"] == "not_found", res
    blob = os.urandom(5000)
    b64 = base64.b64encode(blob).decode()
    res = await tool("artifact_asset_put", artifact_id="cook", path="models/base.stl", data_base64=b64[:4000], final=False)
    assert res == {"path": "models/base.stl", "received": 3000, "final": False}, res
    res = await tool("artifact_asset_put", artifact_id="cook", path="models/base.stl", data_base64=b64[4000:],
                     offset=3000, final=True, sha256=hashlib.sha256(blob).hexdigest())
    assert res["final"] and res["size"] == 5000 and res["content_type"] == "model/stl", res
    res = await tool("artifact_asset_put", artifact_id="cook", path="x.png", data_base64="not base64!!")
    assert res["error"] == "invalid", res

    inbox = assets.inbox
    inbox.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("img/plan.webp", b"RIFF....WEBP")
        zf.writestr("data/parts.json", b'{"n": 2}')
    (inbox / "cook.zip").write_bytes(buf.getvalue())
    res = await tool("artifact_assets_import", artifact_id="cook", source="inbox:cook.zip", prefix="pack")
    assert res["imported"] == 2 and res["count"] == 3 and not (inbox / "cook.zip").exists(), res
    res = await tool("artifact_assets_import", artifact_id="cook", source="/etc/passwd")
    assert res["error"] == "invalid", res

    from aiohttp import web as aweb  # serve a zip on localhost, like a LAN share would
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("from-url.txt", b"over http")
    app = aweb.Application()
    app.router.add_get("/bundle.zip", lambda request: aweb.Response(body=zbuf.getvalue()))
    runner = aweb.AppRunner(app)
    await runner.setup()
    site = aweb.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    import aiohttp
    from custom_components.html_artifacts import llm_api as llm_mod
    plain_session = aiohttp.ClientSession()
    llm_mod.async_get_clientsession = lambda _hass: plain_session  # the test hass has no network component
    res = await tool("artifact_assets_import", artifact_id="cook", source=f"http://127.0.0.1:{port}/bundle.zip", prefix="web")
    assert res["imported"] == 1, res
    res = await tool("artifact_assets_import", artifact_id="cook", source=f"http://127.0.0.1:{port}/missing.zip")
    assert res["error"] == "invalid" and "404" in res["message"], res
    await runner.cleanup()
    await plain_session.close()
    res = await tool("artifact_asset_delete", artifact_id="cook", path="web/from-url.txt")
    assert res["removed"] == 1, res
    res = await tool("artifact_assets_list", artifact_id="cook")
    assert [f["path"] for f in res["files"]] == ["models/base.stl", "pack/data/parts.json", "pack/img/plan.webp"], res
    r = await ws("assets/list", artifact_id="cook")
    assert r["success"] and r["result"]["count"] == 3, r
    ok("MCP assets: chunked base64 put with sha256, zip import from the inbox and from a URL, delete, list (ws too)")

    class AReq(dict):
        def __init__(self, body=b"", admin=True, query=None, headers=None):
            super().__init__(hass_user=SimpleNamespace(name="Script", is_admin=admin))
            self.content_length = len(body)
            self.query = query or {}
            self.headers = headers or {}
            stream = io.BytesIO(body)

            class _C:
                async def read(self, n=-1):
                    return stream.read(n)

            self.content = _C()

    aview = AssetView(hass)
    resp = await aview.get(AReq(), "cook", "models/base.stl")
    assert resp.status == 200 and resp.body == blob and resp.content_type == "model/stl", resp
    etag = resp.headers["ETag"]
    resp = await aview.get(AReq(headers={"If-None-Match": etag}), "cook", "models/base.stl")
    assert resp.status == 304
    resp = await aview.put(AReq(b"hello"), "cook", "notes.txt")
    assert resp.status == 200
    resp = await aview.get(AReq(), "cook", "../../etc/passwd.txt")
    assert resp.status == 400, resp.status
    resp = await aview.get(AReq(), "cook", "nope.png")
    assert resp.status == 404
    resp = await aview.put(AReq(b"x"), "ghost", "a.txt")
    assert resp.status == 404
    resp = await AssetListView(hass).get(AReq(), "cook")
    assert resp.status == 200 and b'"count": 4' in resp.body.replace(b'"count":4', b'"count": 4'), resp.body[:200]
    resp = await aview.delete(AReq(), "cook", "notes.txt")
    assert resp.status == 200
    ok("REST assets: get with ETag/304, put, delete, path validation, 404s")

    # --- REST views ----------------------------------------------------------------------
    class Req(dict):
        def __init__(self, body=None, admin=True):
            super().__init__(hass_user=SimpleNamespace(name="Script", is_admin=admin))
            self._body = body

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
    resp = await listing.get(Req())
    assert resp.status == 200
    resp = await view.get(Req(), "missing")
    assert resp.status == 404
    resp = await view.delete(Req(admin=False), "rest-page")
    assert resp.status == 403
    resp = await view.delete(Req(admin=True), "rest-page")
    assert resp.status == 200
    ok("REST: create, 409 on missing base_version, read, list, 404, admin-only delete")

    # delete cascades to timers
    asset_dir = assets.root / "artifacts" / "cook"
    assert asset_dir.is_dir()
    await ws("delete", artifact_id="cook")
    assert timers.list("cook") == []
    await hass.async_block_till_done()
    assert not asset_dir.exists(), "asset folder removed with the artifact"
    ok("deleting an artifact deletes its timers and asset files")

    timers.async_shutdown()
    await hass.async_stop(force=True)
    print(f"\n{len(PASSED)} smoke checks passed against real Home Assistant")


asyncio.run(main())
