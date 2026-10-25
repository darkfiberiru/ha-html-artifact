"""Real-HA check of the update entity (GitHub is faked).

    /tmp/hav/bin/python tests/smoke_update.py
"""
import asyncio
import io
import json
import pathlib
import sys
import tarfile
import tempfile
import threading
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from homeassistant.components import persistent_notification  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from homeassistant.helpers import frame  # noqa: E402

from custom_components.html_artifacts import update as upd  # noqa: E402
from custom_components.html_artifacts.const import CONF_AUTO_RESTART, CONF_GITHUB_TOKEN, CONF_REPO, DOMAIN, VERSION  # noqa: E402
from custom_components.html_artifacts.updater import Release, UpdateError  # noqa: E402


def tarball(version):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in {
            f"r/custom_components/{DOMAIN}/manifest.json": json.dumps({"domain": DOMAIN, "version": version}),
            f"r/custom_components/{DOMAIN}/__init__.py": "# new",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(text.encode())
            tar.addfile(info, io.BytesIO(text.encode()))
    return buf.getvalue()


def ok(name):
    print("  ok ", name)


async def main():
    upd.async_get_clientsession = lambda hass: object()  # a bare test HA can't build the shared session
    config_dir = tempfile.mkdtemp()
    hass = HomeAssistant(config_dir)
    hass.loop_thread_id = threading.get_ident()
    frame.async_setup(hass)
    comp = pathlib.Path(config_dir, "custom_components", DOMAIN)
    comp.mkdir(parents=True)
    (comp / "__init__.py").write_text("# old")

    entry = SimpleNamespace(entry_id="e1", options={CONF_REPO: "o/r", CONF_GITHUB_TOKEN: "tok"}, add_update_listener=lambda cb: (lambda: None))
    ent = upd.HtmlArtifactsUpdate(entry)
    ent.hass = hass
    ent.entity_id = "update.html_artifacts_update"
    ent.async_write_ha_state = lambda: None

    # nothing checked yet: up to date
    assert ent.installed_version == VERSION and ent.latest_version == VERSION and ent.state == "off"

    seen = {}

    async def fake_fetch(session, repo, token, domain):
        seen.update(repo=repo, token=token)
        return Release("9.9.9", "https://x/tar", "## changes", "https://x/rel")

    with mock.patch.object(upd, "fetch_latest", fake_fetch):
        await ent.async_update()
    assert seen == {"repo": "o/r", "token": "tok"}, seen
    assert ent.latest_version == "9.9.9" and ent.state == "on", (ent.latest_version, ent.state)
    assert await ent.async_release_notes() == "## changes"
    assert ent.release_url == "https://x/rel"
    ok("check finds a newer version and uses configured repo + token")

    # failed check keeps the last release and reports the error
    async def boom(*a):
        raise UpdateError("nope")

    with mock.patch.object(upd, "fetch_latest", boom):
        await ent.async_update()
    assert ent.latest_version == "9.9.9" and ent.extra_state_attributes["last_error"] == "nope"
    ok("failed check keeps last result and surfaces the error")

    # install
    async def fake_download(session, url, token):
        assert url == "https://x/tar" and token == "tok"
        return tarball("9.9.9")

    with mock.patch.object(upd, "download", fake_download):
        await ent.async_install(None, False)
    assert (comp / "__init__.py").read_text() == "# new"
    assert ent.installed_version == "9.9.9" and ent.state == "off" and ent.extra_state_attributes["restart_required"]
    notes = persistent_notification._async_get_or_create_notifications(hass) if hasattr(persistent_notification, "_async_get_or_create_notifications") else None
    if notes is not None:
        assert f"{DOMAIN}_restart" in notes, list(notes)
    ok("install swaps files, marks restart required, posts the restart notification")

    # nothing newer -> install refused
    try:
        await ent.async_install(None, False)
        raise AssertionError("should refuse")
    except HomeAssistantError:
        ok("install refused when already current")

    # auto restart
    ent2 = upd.HtmlArtifactsUpdate(SimpleNamespace(entry_id="e2", options={CONF_AUTO_RESTART: True}, add_update_listener=entry.add_update_listener))
    calls = []

    async def fake_call(domain, service, *a, **k):
        calls.append((domain, service))

    # HA's service registry is read-only, so hand the entity a thin stand-in
    ent2.hass = SimpleNamespace(config=hass.config, async_add_executor_job=hass.async_add_executor_job, services=SimpleNamespace(async_call=fake_call))
    ent2.entity_id, ent2.async_write_ha_state = "update.x2", (lambda: None)
    ent2._release = Release("10.0.0", "https://x/tar", "", "")
    (comp / "manifest.json").write_text("{}")

    async def fake_download2(session, url, token):
        return tarball("10.0.0")

    with mock.patch.object(upd, "download", fake_download2):
        await ent2.async_install(None, False)
    assert calls == [("homeassistant", "restart")], calls
    ok("auto_restart option calls homeassistant.restart")

    # download failure surfaces as a HomeAssistantError and leaves files alone
    ent3 = upd.HtmlArtifactsUpdate(entry)
    ent3.hass, ent3.entity_id, ent3.async_write_ha_state = hass, "update.x3", (lambda: None)
    ent3._release = Release("99.0.0", "https://x/tar", "", "")

    async def bad_download(*a):
        raise UpdateError("Could not download the update (HTTP 404); check the GitHub token.")

    before = (comp / "__init__.py").read_text()
    with mock.patch.object(upd, "download", bad_download):
        try:
            await ent3.async_install(None, False)
            raise AssertionError("should raise")
        except HomeAssistantError as err:
            assert "check the GitHub token" in str(err)
    assert (comp / "__init__.py").read_text() == before and ent3.installed_version == VERSION
    ok("download failure raises a readable error and changes nothing")
    # HACS owns the repo -> built-in updater stands down and removes its stale entity
    (pathlib.Path(config_dir) / ".storage").mkdir()
    (pathlib.Path(config_dir) / ".storage" / "hacs.repositories").write_text(
        json.dumps({"data": {"1": {"full_name": "o/r", "installed": True}}}))
    removed, added = [], []
    reg = SimpleNamespace(async_get_entity_id=lambda d, p, u: "update.html_artifacts_update_2", async_remove=removed.append)
    with mock.patch.object(upd.er, "async_get", lambda h: reg):
        await upd.async_setup_entry(hass, entry, lambda ents, upd_first=False: added.extend(ents))
    assert added == [] and removed == ["update.html_artifacts_update_2"], (added, removed)
    ok("HACS-managed repo: no built-in update entity, stale one removed")
    forced = SimpleNamespace(entry_id="e1", options={CONF_REPO: "o/r", "force_self_update": True}, add_update_listener=entry.add_update_listener)
    await upd.async_setup_entry(hass, forced, lambda ents, upd_first=False: added.extend(ents))
    assert len(added) == 1
    ok("force_self_update keeps the built-in entity")
    (pathlib.Path(config_dir) / ".storage" / "hacs.repositories").unlink()
    added.clear()
    await upd.async_setup_entry(hass, entry, lambda ents, upd_first=False: added.extend(ents))
    assert len(added) == 1
    ok("no HACS: built-in entity is created")
    print("\nupdate entity checks passed")


asyncio.run(main())
