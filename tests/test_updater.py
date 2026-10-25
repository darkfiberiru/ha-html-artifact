import asyncio
import base64
import io
import json
import pathlib
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "html_artifacts"))
import updater  # noqa: E402
from updater import UpdateError, fetch_latest, install_component, is_newer, parse_version  # noqa: E402

DOMAIN = "html_artifacts"


def make_tar(files, root="owner-repo-abc123", extra=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(f"{root}/{name}")
            data = content.encode()
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info)
    return buf.getvalue()


def good_files(version="0.2.0", domain=DOMAIN):
    return {
        f"custom_components/{DOMAIN}/manifest.json": json.dumps({"domain": domain, "version": version}),
        f"custom_components/{DOMAIN}/__init__.py": "# new",
        f"custom_components/{DOMAIN}/frontend/a.js": "js",
        "README.md": "ignored",
    }


class FakeResp:
    def __init__(self, status, body=None, text=None, headers=None):
        self.status, self._body, self._text, self.headers = status, body, text, headers or {}

    async def json(self):
        return self._body

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, headers=None, **kw):
        self.calls.append((url, headers))
        for suffix, resp in self.routes.items():
            if url.endswith(suffix):
                return resp
        return FakeResp(404)


def run(coro):
    return asyncio.run(coro)


class Versions(unittest.TestCase):
    def test_parse_and_compare(self):
        self.assertEqual(parse_version("v1.2.3"), (1, 2, 3))
        self.assertTrue(is_newer("0.2.0", "0.1.9"))
        self.assertTrue(is_newer("v1.0.1", "1.0"))
        self.assertFalse(is_newer("1.0", "1.0.0"))
        self.assertFalse(is_newer("0.1.0", "0.2.0"))
        self.assertFalse(is_newer("garbage", "0.1.0"))
        self.assertTrue(is_newer("0.10.0", "0.9.0"))  # numeric, not string, comparison


class Fetch(unittest.TestCase):
    def test_uses_latest_release_and_sends_token(self):
        s = FakeSession({
            "/repos/o/r": FakeResp(200, {"default_branch": "main"}),
            "/releases/latest": FakeResp(200, {"tag_name": "v0.3.0", "tarball_url": "https://x/tar", "body": "notes", "html_url": "https://x/rel"}),
        })
        rel = run(fetch_latest(s, "o/r", "tok", DOMAIN))
        self.assertEqual((rel.version, rel.tarball_url, rel.notes), ("0.3.0", "https://x/tar", "notes"))
        self.assertTrue(all(h["Authorization"] == "Bearer tok" for _, h in s.calls))

    def test_falls_back_to_manifest_on_default_branch(self):
        s = FakeSession({
            "/repos/o/r": FakeResp(200, {"default_branch": "trunk"}),
            "/releases/latest": FakeResp(404),
            "manifest.json?ref=trunk": FakeResp(200, text=json.dumps({"version": "0.4.0"})),
        })
        rel = run(fetch_latest(s, "o/r", None, DOMAIN))
        self.assertEqual(rel.version, "0.4.0")
        self.assertTrue(rel.tarball_url.endswith("/repos/o/r/tarball/trunk"))
        self.assertTrue(all("Authorization" not in h for _, h in s.calls))

    def test_private_repo_without_token_explains(self):
        with self.assertRaises(UpdateError) as ctx:
            run(fetch_latest(FakeSession({}), "o/r", None, DOMAIN))
        self.assertIn("private", str(ctx.exception))

    def test_auth_and_rate_limit_errors(self):
        with self.assertRaisesRegex(UpdateError, "check the GitHub token"):
            run(fetch_latest(FakeSession({"/repos/o/r": FakeResp(401)}), "o/r", "bad", DOMAIN))
        with self.assertRaisesRegex(UpdateError, "rate limit"):
            run(fetch_latest(FakeSession({"/repos/o/r": FakeResp(403, headers={"X-RateLimit-Remaining": "0"})}), "o/r", None, DOMAIN))

    def test_rejects_bad_repo_name(self):
        for bad in ("", "nope", "a/b/c", "../x", "a b/c"):
            with self.assertRaises(UpdateError):
                run(fetch_latest(FakeSession({}), bad, None, DOMAIN))


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.dest = self.tmp / DOMAIN
        self.dest.mkdir()
        (self.dest / "__init__.py").write_text("# old")
        (self.dest / "stale.py").write_text("stale")

    def test_replaces_component_only(self):
        version = install_component(make_tar(good_files()), DOMAIN, self.dest)
        self.assertEqual(version, "0.2.0")
        self.assertEqual((self.dest / "__init__.py").read_text(), "# new")
        self.assertTrue((self.dest / "frontend" / "a.js").exists())
        self.assertFalse((self.dest / "stale.py").exists(), "removed files must not linger")
        self.assertFalse((self.dest / "README.md").exists())
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [DOMAIN], "no staging/backup dirs left")

    def _assert_untouched(self):
        self.assertEqual((self.dest / "__init__.py").read_text(), "# old")
        self.assertTrue((self.dest / "stale.py").exists())
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [DOMAIN])

    def test_rejects_invalid_archives_without_touching_install(self):
        with self.assertRaises(UpdateError):
            install_component(b"not a tarball", DOMAIN, self.dest)
        self._assert_untouched()
        files = good_files()
        del files[f"custom_components/{DOMAIN}/__init__.py"]
        with self.assertRaisesRegex(UpdateError, "__init__.py"):
            install_component(make_tar(files), DOMAIN, self.dest)
        self._assert_untouched()
        with self.assertRaisesRegex(UpdateError, "not this integration"):
            install_component(make_tar(good_files(domain="other")), DOMAIN, self.dest)
        self._assert_untouched()
        with self.assertRaisesRegex(UpdateError, "no custom_components"):
            install_component(make_tar({"README.md": "x"}), DOMAIN, self.dest)
        self._assert_untouched()

    def test_rejects_links_and_traversal(self):
        link = tarfile.TarInfo(f"r/custom_components/{DOMAIN}/evil")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        with self.assertRaisesRegex(UpdateError, "link"):
            install_component(make_tar(good_files(), root="r", extra=[link]), DOMAIN, self.dest)
        self._assert_untouched()
        files = good_files()
        files[f"custom_components/{DOMAIN}/../../../escape.txt"] = "x"
        with self.assertRaisesRegex(UpdateError, "unsafe"):
            install_component(make_tar(files), DOMAIN, self.dest)
        self._assert_untouched()
        self.assertFalse((self.tmp.parent / "escape.txt").exists())

    def test_size_cap(self):
        original = updater.MAX_UNPACKED
        updater.MAX_UNPACKED = 5
        try:
            with self.assertRaisesRegex(UpdateError, "50 MB"):
                install_component(make_tar(good_files()), DOMAIN, self.dest)
        finally:
            updater.MAX_UNPACKED = original
        self._assert_untouched()

    def test_refuses_symlinked_install(self):
        real = self.tmp / "real"
        real.mkdir()
        link = self.tmp / "linked"
        link.symlink_to(real)
        with self.assertRaisesRegex(UpdateError, "git pull"):
            install_component(make_tar(good_files()), DOMAIN, link)

    def test_fresh_install_when_missing(self):
        fresh = self.tmp / "fresh" / DOMAIN
        fresh.parent.mkdir()
        self.assertEqual(install_component(make_tar(good_files("1.0.0")), DOMAIN, fresh), "1.0.0")

    def test_swap_failure_restores_old_copy(self):
        real_rename = pathlib.Path.rename

        def flaky(self_, target):
            if self_.name == f"_{DOMAIN}_new":
                raise OSError("disk says no")
            return real_rename(self_, target)

        pathlib.Path.rename = flaky
        try:
            with self.assertRaises(OSError):
                install_component(make_tar(good_files()), DOMAIN, self.dest)
        finally:
            pathlib.Path.rename = real_rename
        self._assert_untouched()


class ManifestInSync(unittest.TestCase):
    def test_const_version_matches_manifest(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "html_artifacts"
        manifest = json.loads((root / "manifest.json").read_text())["version"]
        const = [l for l in (root / "const.py").read_text().splitlines() if l.startswith("VERSION")][0]
        self.assertIn(f'"{manifest}"', const)


if __name__ == "__main__":
    unittest.main()
