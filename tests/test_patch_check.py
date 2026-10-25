"""Edits, patching, lint and render reports (pure core, no Home Assistant)."""
import sys
import unittest

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from test_core import FakeStore, core  # noqa: E402


def make_store():
    store = core.ArtifactStore(FakeStore())
    store.save("page", "<html><body><h1>Title</h1><p>one</p><p>two</p></body></html>", title="Page", source="claude", updated_by="claude")
    return store


class ApplyEdits(unittest.TestCase):
    def test_applies_in_order_and_is_exact(self):
        out = core.apply_edits("a b c", [{"old_str": "b", "new_str": "B"}, {"old_str": "B c", "new_str": "BC"}])
        self.assertEqual(out, "a BC")

    def test_rejects_missing_and_ambiguous_matches(self):
        with self.assertRaises(core.ArtifactError) as ctx:
            core.apply_edits("x y x", [{"old_str": "x", "new_str": "z"}])
        self.assertEqual((ctx.exception.code, ctx.exception.extra["matches"]), ("edit_failed", 2))
        self.assertIn("unique", ctx.exception.message)
        with self.assertRaises(core.ArtifactError) as ctx:
            core.apply_edits("abc", [{"old_str": "nope", "new_str": "z"}])
        self.assertEqual(ctx.exception.extra["matches"], 0)
        self.assertIn("whitespace", ctx.exception.message)

    def test_invalid_shapes(self):
        for bad in (None, [], "x", [1], [{"old_str": "", "new_str": "x"}], [{"old_str": "a"}],
                    [{"old_str": "a", "new_str": "a"}], [{"old_str": "a", "new_str": "b"}] * (core.MAX_EDITS + 1)):
            with self.assertRaises(core.ArtifactError, msg=repr(bad)[:40]) as ctx:
                core.apply_edits("a", bad)
            self.assertEqual(ctx.exception.code, "invalid")

    def test_second_edit_failure_applies_nothing(self):
        store = make_store()
        with self.assertRaises(core.ArtifactError):
            store.patch("page", [{"old_str": "one", "new_str": "1"}, {"old_str": "missing", "new_str": "x"}],
                        base_version=1, source="claude", updated_by="claude")
        self.assertIn("one", store.get("page")["html"])
        self.assertEqual(store.get("page")["version"], 1)


class Patch(unittest.TestCase):
    def test_patch_creates_a_new_version_and_keeps_history(self):
        store = make_store()
        out = store.patch("page", [{"old_str": "<p>one</p>", "new_str": "<p>uno</p>"}],
                          base_version=1, source="claude", updated_by="claude")
        self.assertEqual(out["version"], 2)
        self.assertIn("<p>uno</p>", out["html"])
        self.assertIn("<p>two</p>", out["html"])
        self.assertEqual([h["version"] for h in store.history("page")], [1])

    def test_base_version_is_required_and_checked_before_applying(self):
        store = make_store()
        store.save("page", "<p>hand edit</p>", base_version=1, source="user", updated_by="user:alex")
        with self.assertRaises(core.ArtifactError) as ctx:
            store.patch("page", [{"old_str": "<p>one</p>", "new_str": "x"}], base_version=1, source="claude")
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertEqual(ctx.exception.extra["current"]["updated_by"], "user:alex")
        self.assertIn("hand edit", ctx.exception.extra["current"]["html"])
        with self.assertRaises(core.ArtifactError) as ctx:
            store.patch("page", [{"old_str": "hand", "new_str": "x"}], base_version=None, source="claude")
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertNotIn("html", ctx.exception.extra["current"], "a missing base_version must not dump the page")

    def test_unknown_artifact_and_capability_update(self):
        store = make_store()
        with self.assertRaises(core.ArtifactError) as ctx:
            store.patch("nope", [{"old_str": "a", "new_str": "b"}], base_version=1, source="claude")
        self.assertEqual(ctx.exception.code, "not_found")
        out = store.patch("page", [{"old_str": "two", "new_str": "2"}], base_version=1,
                          caps_requested={"notify": True}, title="Renamed", source="claude")
        self.assertEqual((out["title"], out["caps_requested"]["notify"]), ("Renamed", True))

    def test_summary_reports_size_in_bytes(self):
        store = core.ArtifactStore(FakeStore())
        store.save("p", "héllo", source="user")
        self.assertEqual(store.list()[0]["size"], len("héllo".encode()))
        self.assertEqual(store.version_of("p"), 1)
        self.assertIsNone(store.version_of("zz"))


class Lint(unittest.TestCase):
    def codes(self, html, caps=None):
        return {w["code"] for w in core.lint_html(html, caps)}

    def test_flags_sandbox_problems(self):
        html = ('<meta name="viewport" content="width=device-width"><script src="https://evil.example/x.js"></script>'
                '<link rel="stylesheet" href="https://example.org/a.css"><script>localStorage.a=1;fetch("/x");window.open("/")</script>'
                '<img src="https://example.org/i.png"><script>ha.kv.get("a")</script>')
        self.assertEqual(self.codes(html), {"storage", "network", "script_src", "link_href", "external_media", "popup", "ha_ready"})

    def test_allowed_hosts_and_granted_network_are_fine(self):
        html = ('<meta name="viewport" content="x"><script src="https://cdn.jsdelivr.net/npm/a@1/a.js"></script>'
                '<link href="https://fonts.googleapis.com/css2?family=Inter" rel="stylesheet"><script>await ha.ready; fetch("https://api.example")</script>')
        self.assertEqual(self.codes(html, {"network": True}), set())
        self.assertIn("network", self.codes(html))

    def test_commented_out_code_is_ignored_and_unbalanced_scripts_flagged(self):
        self.assertNotIn("storage", self.codes("<!-- localStorage.x -->"))
        self.assertIn("script_tag", self.codes("<script>var a = 1;"))

    def test_clean_page_only_suggests_viewport(self):
        self.assertEqual(self.codes("<p>hi</p>"), {"viewport"})


class Reports(unittest.TestCase):
    def test_collects_dedupes_and_resets_on_new_version(self):
        reports = core.RenderReports(clock=lambda: 5.0)
        events = []
        reports.add_listener(events.append)
        reports.add("p", 1, source="dry-run", settled=False, errors=[{"level": "error", "message": "boom", "line": 12}])
        reports.add("p", 1, source="viewer", settled=True, errors=[{"level": "error", "message": "boom"}, {"level": "warning", "message": "csp"}])
        r = reports.summary("p", 1)
        self.assertEqual((r["renders"], r["settled"], r["sources"]), (1, True, ["dry-run", "viewer"]))
        self.assertEqual([(e["level"], e["message"]) for e in r["errors"]], [("error", "boom"), ("warning", "csp")])
        self.assertEqual(r["errors"][0]["line"], 12)
        self.assertEqual(len(events), 2)
        self.assertIsNone(reports.summary("p", 2), "reports are per version")
        reports.add("p", 2, source="dry-run", settled=True)
        self.assertEqual(reports.summary("p", 2)["errors"], [])
        self.assertIsNone(reports.summary("p", 1), "an older version's report is dropped")

    def test_limits_and_junk(self):
        reports = core.RenderReports()
        junk = ["x", 3, None, {"message": ""}, {"level": "error", "message": "m" * 900, "line": -4}]
        many = [{"level": "error", "message": f"e{i}"} for i in range(80)]
        reports.add("p", 1, source="viewer", settled=True, errors=junk + many)
        r = reports.summary("p", 1)
        self.assertEqual(len(r["errors"]), core.MAX_REPORT_ERRORS)
        self.assertEqual(len(r["errors"][0]["message"]), 500)
        self.assertNotIn("line", r["errors"][0])
        reports.forget("p")
        self.assertIsNone(reports.summary("p", 1))


if __name__ == "__main__":
    unittest.main()
