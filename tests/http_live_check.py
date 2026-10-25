"""Live HTTP check: real Home Assistant http server + real curl (optional).

    python -m venv /tmp/hav && /tmp/hav/bin/pip install homeassistant
    cd <repo> && /tmp/hav/bin/python tests/http_live_check.py

Starts HA's `http` component on a free local port (with its auth and ban
middleware), registers this integration's views, then uses `curl` for the
signed upload URL and token-authenticated asset requests.
"""
import asyncio
import io
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from homeassistant.core import Context, HomeAssistant  # noqa: E402
from homeassistant.helpers import frame, llm  # noqa: E402
from homeassistant.helpers.storage import Store  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402

from custom_components.html_artifacts import Runtime, make_scheduler  # noqa: E402
from custom_components.html_artifacts.assets import AssetStore  # noqa: E402
from custom_components.html_artifacts.const import DOMAIN  # noqa: E402
from custom_components.html_artifacts.core import ArtifactStore, TimerEngine  # noqa: E402
from custom_components.html_artifacts.http import async_register_http  # noqa: E402
from custom_components.html_artifacts.llm_api import ArtifactsAPI  # noqa: E402

PASSED = []


def ok(name):
    PASSED.append(name)
    print("  ok ", name)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def curl(*args, data=None):
    """Run curl in a thread (the HA loop must keep serving). Returns (status, body)."""
    def run():
        cmd = ["curl", "-sS", "-o", "-", "-w", "\n%{http_code}", *args]
        out = subprocess.run(cmd, input=data, capture_output=True, check=True).stdout
        body, _, code = out.rpartition(b"\n")
        return int(code), body
    return await asyncio.get_running_loop().run_in_executor(None, run)


async def main():
    hass = HomeAssistant(tempfile.mkdtemp())
    hass.loop_thread_id = threading.get_ident()
    frame.async_setup(hass)
    from homeassistant import auth as ha_auth, loader
    loader.async_setup(hass)
    hass.auth = await ha_auth.auth_manager_from_config(hass, [{"type": "homeassistant"}], [])
    from homeassistant import config_entries
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    port = free_port()
    assert await async_setup_component(hass, "http", {"http": {"server_host": ["127.0.0.1"], "server_port": port}})
    await hass.async_start()
    hass.config.internal_url = f"http://127.0.0.1:{port}"

    entry = SimpleNamespace(options={})
    artifacts = ArtifactStore(Store(hass, 1, f"{DOMAIN}.artifacts"))
    timers = TimerEngine(Store(hass, 1, f"{DOMAIN}.timers"), time.time, make_scheduler(hass))
    from custom_components.html_artifacts.notifier import TimerNotifier
    notifier = TimerNotifier(hass, artifacts, entry)
    assets = AssetStore(hass.config.path(DOMAIN))
    hass.data[DOMAIN] = {"runtime": Runtime(artifacts, timers, notifier, assets, hass)}
    async_register_http(hass)
    artifacts.save("vent", "<p>page</p>", title="Vent")

    api = ArtifactsAPI(hass)
    inst = await api.async_get_api_instance(
        llm.LLMContext(platform="test", context=Context(), language="en", assistant=None, device_id=None)
    )

    async def tool(name, **args):
        return await inst.async_call_tool(llm.ToolInput(tool_name=name, tool_args=args))

    base = f"http://127.0.0.1:{port}"

    # 1. the normal asset API still needs auth
    code, _ = await curl(f"{base}/api/html_artifacts/vent/assets")
    assert code == 401, code
    ok("asset API without a token: 401 from HA's auth middleware")

    # 2. signed upload of a ~9 MB zip with plain curl, no auth header
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for i in range(3):
            zf.writestr(f"models/part{i}.stl", os.urandom(3_000_000))
        zf.writestr("img/plan.webp", os.urandom(50_000))
    blob = buf.getvalue()
    tmp = pathlib.Path(tempfile.mkdtemp()) / "bundle.zip"
    tmp.write_bytes(blob)
    res = await tool("artifact_assets_upload_url", artifact_id="vent", zip=True)
    url = res["urls"][0]
    assert url.startswith(base + "/api/html_artifacts/upload/"), res
    code, body = await curl("-X", "PUT", "--data-binary", f"@{tmp}", url)
    assert code == 200, (code, body[:300])
    result = json.loads(body)
    assert result["imported"] == 4 and result["total_bytes"] == 9_050_000, result
    ok(f"curl PUT of a {len(blob) / 1e6:.1f} MB zip to the signed URL, no auth header: imported 4 files")

    code, body = await curl("-X", "PUT", "--data-binary", f"@{tmp}", url)
    assert code == 403, (code, body)
    ok("the same URL again: 403 (single use)")

    code, _ = await curl("-X", "PUT", "--data-binary", "x", f"{base}/api/html_artifacts/upload/not-a-real-token")
    assert code == 403, code
    ok("a made-up token: 403")

    # 3. single file with sha256, via the signed URL
    import hashlib
    payload = os.urandom(12345)
    f1 = tmp.with_name("one.bin")
    f1.write_bytes(payload)
    res = await tool("artifact_assets_upload_url", artifact_id="vent", path="data/one.bin",
                     sha256=hashlib.sha256(payload).hexdigest())
    code, body = await curl("-X", "PUT", "--data-binary", f"@{f1}", res["urls"][0])
    assert code == 200 and json.loads(body)["size"] == 12345, (code, body)
    ok("curl PUT of one file with sha256 check")

    # 4. a file larger than HA's 16 MB client_max_size, streamed in (zip limit is 128 MB)
    big = tmp.with_name("big.zip")
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w", zipfile.ZIP_STORED) as zf:
        for i in range(3):
            zf.writestr(f"big/b{i}.bin", os.urandom(6_000_000))
    big.write_bytes(zb.getvalue())
    res = await tool("artifact_assets_upload_url", artifact_id="vent", zip=True, prefix="huge")
    code, body = await curl("-X", "PUT", "--data-binary", f"@{big}", res["urls"][0])
    assert code == 200, (code, body[:300])
    ok(f"curl PUT of an {big.stat().st_size / 1e6:.1f} MB zip (over HA's 16 MB request cap for read())")

    # 5. read back over the authenticated API with a real access token
    user = await hass.auth.async_create_user("tester", group_ids=["system-admin"])
    refresh = await hass.auth.async_create_refresh_token(user, "http://test/")
    token = hass.auth.async_create_access_token(refresh)
    code, body = await curl("-H", f"Authorization: Bearer {token}", f"{base}/api/html_artifacts/vent/assets/data/one.bin")
    assert code == 200 and body == payload, (code, len(body))
    code, body = await curl("-H", f"Authorization: Bearer {token}", f"{base}/api/html_artifacts/vent/assets")
    listing = json.loads(body)
    assert listing["count"] == 8, listing
    ok("authenticated GET returns the exact bytes; list shows all 8 files")

    mb = os.urandom(5_000_000)
    f2 = tmp.with_name("five.bin")
    f2.write_bytes(mb)
    code, body = await curl("-X", "PUT", "-H", f"Authorization: Bearer {token}", "--data-binary", f"@{f2}",
                            f"{base}/api/html_artifacts/vent/assets/data/five.bin")
    assert code == 200 and json.loads(body)["size"] == 5_000_000, (code, body[:200])
    code, body = await curl("-H", f"Authorization: Bearer {token}", f"{base}/api/html_artifacts/vent/assets/data/five.bin")
    assert code == 200 and body == mb
    ok("authenticated REST PUT of a 5 MB file round-trips byte for byte")

    await hass.async_stop(force=True)
    print(f"\n{len(PASSED)} live HTTP checks passed against a real Home Assistant http server")


asyncio.run(main())
