"""Artifact display type (card / page / both) and card_height."""
import unittest

from tests.test_core import make_store, core


def new(store, aid="a", **kw):
    return store.save(aid, "<p>x</p>", source="claude", **kw)


class DisplayTest(unittest.TestCase):
    def setUp(self):
        out = make_store()
        self.store = out[0] if isinstance(out, tuple) else out

    def test_new_artifacts_default_to_page(self):
        a = new(self.store)
        self.assertEqual(a["display"], "page")
        self.assertIsNone(a["card_height"])

    def test_given_values_are_stored_and_listed(self):
        new(self.store, display="card", card_height=220)
        new(self.store, "b", display="both", card_height="auto")
        by_id = {a["id"]: a for a in self.store.list()}
        self.assertEqual((by_id["a"]["display"], by_id["a"]["card_height"]), ("card", 220))
        self.assertEqual((by_id["b"]["display"], by_id["b"]["card_height"]), ("both", "auto"))

    def test_artifacts_saved_before_the_field_are_both(self):
        new(self.store)
        for artifact in self.store._data["artifacts"].values():
            artifact.pop("display", None)
            artifact.pop("card_height", None)
        self.assertEqual(self.store.get("a")["display"], "both")
        self.assertEqual(self.store.list()[0]["display"], "both")

    def test_only_changing_display_or_height_makes_no_new_version(self):
        a = new(self.store)
        b = self.store.save("a", "<p>x</p>", display="card", card_height=300, source="user", base_version=a["version"])
        self.assertEqual(b["version"], a["version"])
        self.assertEqual((b["display"], b["card_height"]), ("card", 300))
        self.assertEqual(self.store.history("a"), [])
        c = self.store.save("a", "<p>x</p>", card_height="auto", source="user", base_version=1)
        self.assertEqual((c["version"], c["card_height"], c["display"]), (1, "auto", "card"))

    def test_a_change_event_is_emitted_for_settings(self):
        events = []
        self.store.add_listener(events.append)
        a = new(self.store)
        events.clear()
        self.store.save("a", "<p>x</p>", display="card", source="user", base_version=a["version"])
        self.assertEqual([e["change"] for e in events], ["meta"])

    def test_content_change_keeps_the_settings_unless_given(self):
        a = new(self.store, display="card", card_height=200)
        b = self.store.save("a", "<p>y</p>", source="user", base_version=a["version"])
        self.assertEqual((b["version"], b["display"], b["card_height"]), (2, "card", 200))

    def test_patch_accepts_the_settings(self):
        a = new(self.store)
        b = self.store.patch("a", [{"old_str": "x", "new_str": "y"}], base_version=a["version"], display="both", card_height=400)
        self.assertEqual((b["display"], b["card_height"], b["version"]), ("both", 400, 2))

    def test_validation(self):
        for kw in ({"display": "tile"}, {"card_height": 50}, {"card_height": 9000}, {"card_height": "tall"},
                   {"card_height": True}):
            with self.assertRaises(core.ArtifactError, msg=str(kw)):
                new(self.store, **kw)
        self.assertEqual(new(self.store, card_height="300")["card_height"], 300)


class CardLintTest(unittest.TestCase):
    def test_card_with_a_much_taller_fixed_height_warns(self):
        html = "<style>.x{min-height: 900px}</style><meta name=viewport content=x>"
        codes = [w["code"] for w in core.lint_html(html, None, "card", 300)]
        self.assertIn("card_tall", codes)

    def test_fitting_card_page_and_auto_do_not_warn(self):
        html = "<style>.x{height: 280px}</style><meta name=viewport content=x>"
        self.assertNotIn("card_tall", [w["code"] for w in core.lint_html(html, None, "card", 300)])
        tall = "<style>.x{height: 900px}</style>"
        self.assertNotIn("card_tall", [w["code"] for w in core.lint_html(tall, None, "page", None)])
        self.assertNotIn("card_tall", [w["code"] for w in core.lint_html(tall, None, "card", "auto")])


class FramesTest(unittest.TestCase):
    def test_reports_keep_one_frame_per_size(self):
        reports = core.RenderReports()
        reports.add("a", 1, source="dry-run", settled=True, frames=[{"w": 420, "h": 300, "content": 500}, {"w": "x"}])
        reports.add("a", 1, source="dry-run", settled=True, frames=[{"w": 420, "h": 300, "content": 280}])
        self.assertEqual(reports.summary("a", 1)["frames"], [{"w": 420, "h": 300, "content": 280}])


if __name__ == "__main__":
    unittest.main()
