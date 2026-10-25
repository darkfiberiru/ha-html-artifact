"""Unit tests for per-artifact asset files (no Home Assistant needed).

Run:  python -m unittest discover -s tests -v
"""
import hashlib
import importlib.util
import io
import pathlib
import stat
import sys
import tempfile
import types
import unittest
import zipfile

PKG_DIR = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "html_artifacts"

# Load core.py and assets.py as a tiny package without importing __init__ (which needs HA).
_pkg = types.ModuleType("haa_pkg")
_pkg.__path__ = [str(PKG_DIR)]
sys.modules.setdefault("haa_pkg", _pkg)


def _load(name):
    full = f"haa_pkg.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, PKG_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


core = _load("core")
assets = _load("assets")
ArtifactError = core.ArtifactError


def make_zip(entries, symlink=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    return buf.getvalue()


class AssetStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.store = assets.AssetStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def code(self, fn, *args, **kwargs):
        with self.assertRaises(ArtifactError) as ctx:
            fn(*args, **kwargs)
        return ctx.exception.code

    # -- paths -----------------------------------------------------------------

    def test_clean_path(self):
        self.assertEqual(assets.clean_path("img/plan.webp"), "img/plan.webp")
        self.assertEqual(assets.clean_path("/models/a.stl"), "models/a.stl")
        for bad in ["", "../x.png", "a/../b.png", ".hidden.png", "a//b.png", "a b.png", "x.exe", "x", "a\\b.png",
                    "/".join(["d"] * 9) + "/x.png", 5]:
            with self.subTest(bad=bad):
                self.assertEqual(self.code(assets.clean_path, bad), "invalid")

    def test_content_types(self):
        self.assertEqual(assets.content_type("a.STL"), "model/stl")
        self.assertTrue(assets.content_type("x.txt").startswith("text/plain"))

    # -- put / read / list / delete ----------------------------------------------

    def test_put_read_list_delete(self):
        entry = self.store.put("vent", "img/a.png", b"\x89PNG data")
        self.assertEqual(entry["size"], 9)
        self.assertEqual(entry["sha256"], hashlib.sha256(b"\x89PNG data").hexdigest())
        self.assertTrue((self.root / "artifacts" / "vent" / "img" / "a.png").is_file())
        data, meta = self.store.read("vent", "img/a.png")
        self.assertEqual(data, b"\x89PNG data")
        self.assertEqual(meta["content_type"], "image/png")
        listing = self.store.list("vent")
        self.assertEqual([f["path"] for f in listing["files"]], ["img/a.png"])
        self.assertEqual(listing["total_bytes"], 9)
        self.assertEqual(self.store.delete("vent", "img/a.png"), 1)
        self.assertFalse((self.root / "artifacts" / "vent" / "img").exists(), "empty folder pruned")
        self.assertEqual(self.code(self.store.read, "vent", "img/a.png"), "not_found")
        self.assertEqual(self.code(self.store.delete, "vent", "img/a.png"), "not_found")

    def test_overwrite_replaces(self):
        self.store.put("vent", "a.txt", b"one")
        self.store.put("vent", "a.txt", b"two!")
        self.assertEqual(self.store.read("vent", "a.txt")[0], b"two!")
        self.assertEqual(self.store.list("vent")["count"], 1)

    def test_chunked_upload_and_resume_errors(self):
        r = self.store.put("vent", "m/base.stl", b"abc", offset=0, final=False)
        self.assertEqual(r, {"path": "m/base.stl", "received": 3, "final": False})
        err = self.code(self.store.put, "vent", "m/base.stl", b"zzz", offset=5, final=False)
        self.assertEqual(err, "conflict")
        self.assertEqual(self.code(self.store.read, "vent", "m/base.stl"), "not_found", "not visible until final")
        digest = hashlib.sha256(b"abcdef").hexdigest()
        done = self.store.put("vent", "m/base.stl", b"def", offset=3, final=True, sha256=digest)
        self.assertTrue(done["final"])
        self.assertEqual(self.store.read("vent", "m/base.stl")[0], b"abcdef")
        self.assertFalse(any((self.root / "artifacts" / "vent" / ".staging").iterdir()), "staging cleaned")

    def test_sha_mismatch_rejected(self):
        self.assertEqual(self.code(self.store.put, "vent", "a.txt", b"x", sha256="00" * 32), "invalid")
        self.assertEqual(self.store.list("vent")["count"], 0)

    def test_limits(self):
        old = (assets.MAX_FILE_BYTES, assets.MAX_TOTAL_BYTES, assets.MAX_FILES)
        try:
            assets.MAX_FILE_BYTES, assets.MAX_TOTAL_BYTES, assets.MAX_FILES = 10, 15, 2
            self.assertEqual(self.code(self.store.put, "vent", "big.bin", b"x" * 11), "too_large")
            self.store.put("vent", "a.bin", b"x" * 10)
            self.assertEqual(self.code(self.store.put, "vent", "b.bin", b"x" * 6), "too_large")
            self.store.put("vent", "b.bin", b"x" * 5)
            self.assertEqual(self.code(self.store.put, "vent", "c.bin", b""), "limit")
            self.store.put("vent", "a.bin", b"y" * 10)  # replacing within limits is fine
        finally:
            assets.MAX_FILE_BYTES, assets.MAX_TOTAL_BYTES, assets.MAX_FILES = old

    def test_bad_artifact_id(self):
        self.assertEqual(self.code(self.store.put, "../x", "a.txt", b"x"), "invalid")

    def test_delete_all_and_index_rebuild(self):
        self.store.put("vent", "a.txt", b"1")
        self.store.put("vent", "d/b.txt", b"22")
        (self.root / "artifacts" / "vent" / ".index.json").unlink()
        self.assertEqual(self.store.list("vent")["total_bytes"], 3, "index rebuilt from disk")
        self.assertEqual(self.store.delete("vent"), 2)
        self.assertFalse((self.root / "artifacts" / "vent").exists())
        self.assertEqual(self.store.list("vent")["count"], 0)

    # -- zip import --------------------------------------------------------------

    def test_import_zip_with_prefix_and_replace(self):
        self.store.put("vent", "old.txt", b"old")
        res = self.store.import_zip("vent", make_zip({"a.png": b"A", "sub/b.json": b"{}", "dir/": b""}), prefix="pack")
        self.assertEqual(res["imported"], 2)
        paths = [f["path"] for f in self.store.list("vent")["files"]]
        self.assertEqual(paths, ["old.txt", "pack/a.png", "pack/sub/b.json"])
        self.store.import_zip("vent", make_zip({"only.txt": b"1"}), replace=True)
        self.assertEqual([f["path"] for f in self.store.list("vent")["files"]], ["only.txt"])
        self.assertFalse((self.root / "artifacts" / "vent" / "old.txt").exists())

    def test_import_zip_refuses_bad_members_without_writing(self):
        cases = {
            "traversal": make_zip({"../evil.txt": b"x"}),
            "bad type": make_zip({"ok.txt": b"1", "run.exe": b"MZ"}),
            "symlink": make_zip({"ok.txt": b"1"}, symlink="link.txt"),
            "not zip": b"definitely not a zip",
        }
        for name, blob in cases.items():
            with self.subTest(name):
                self.assertEqual(self.code(self.store.import_zip, "vent", blob), "invalid")
                self.assertEqual(self.store.list("vent")["count"], 0, "nothing written")

    def test_import_zip_bomb_ratio(self):
        self.assertEqual(self.code(self.store.import_zip, "vent", make_zip({"z.txt": b"0" * 2_000_000})), "invalid")

    def test_import_from_inbox_path(self):
        self.store.inbox.mkdir(parents=True)
        (self.store.inbox / "vent.zip").write_bytes(make_zip({"x.txt": b"hi"}))
        path = self.store.inbox_file("inbox/vent.zip")
        self.assertEqual(self.store.import_zip("vent", path)["imported"], 1)
        self.assertEqual(self.code(self.store.inbox_file, "../secrets.zip"), "invalid")
        self.assertEqual(self.code(self.store.inbox_file, "missing.zip"), "not_found")
        self.assertEqual(self.code(self.store.inbox_file, "vent.tar"), "invalid")


class UploadTicketsTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.t = assets.UploadTickets(clock=lambda: self.now)

    def test_single_use_and_bound(self):
        tk = self.t.issue("vent", mode="file", path="img/a.png", sha256="ab" * 32)
        self.assertEqual((tk["mode"], tk["path"], tk["max_bytes"]), ("file", "img/a.png", assets.MAX_FILE_BYTES))
        self.assertGreaterEqual(len(tk["token"]), 40)
        got = self.t.take(tk["token"])
        self.assertEqual(got["artifact_id"], "vent")
        with self.assertRaises(ArtifactError) as ctx:
            self.t.take(tk["token"])
        self.assertEqual(ctx.exception.code, "forbidden")

    def test_expiry_clamped_and_enforced(self):
        tk = self.t.issue("vent", mode="zip", ttl=5)  # clamped up to 60 s
        self.assertEqual(tk["expires_at"], 1060.0)
        self.now = 1061.0
        with self.assertRaises(ArtifactError):
            self.t.take(tk["token"])
        tk = self.t.issue("vent", mode="zip", ttl=99999)
        self.assertEqual(tk["expires_at"], self.now + assets.UPLOAD_TTL_MAX)

    def test_validation(self):
        for kwargs in ({"mode": "file"}, {"mode": "file", "path": "../x.png"}, {"mode": "dir"},
                       {"mode": "zip", "sha256": "xyz"}):
            with self.subTest(kwargs):
                with self.assertRaises(ArtifactError):
                    self.t.issue("vent", **kwargs)
        with self.assertRaises(ArtifactError):
            self.t.issue("../bad", mode="zip")


if __name__ == "__main__":
    unittest.main()
