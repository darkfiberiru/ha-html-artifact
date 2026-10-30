"""Lovelace resource sync: the decision logic, without needing Home Assistant."""
import importlib.util
import pathlib
import sys
import unittest

# Loaded by path, like tests/test_core.py: importing the package would pull in
# homeassistant, which these tests deliberately do not need.
_SRC = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "html_artifacts" / "resources.py"
_spec = importlib.util.spec_from_file_location("ha_art_resources", _SRC)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["ha_art_resources"] = _mod
_spec.loader.exec_module(_mod)
plan_resource = _mod.plan_resource

PREFIX = "/html_artifacts_static/html-artifacts.js"
URL = PREFIX + "?v=0.9.2"


class PlanResourceTest(unittest.TestCase):
    def test_absent_is_created(self):
        items = [{"id": "1", "url": "/hacsfiles/other/other.js?hacstag=1"}]
        self.assertEqual(plan_resource(items, URL, PREFIX), ("create", None))

    def test_empty_list_is_created(self):
        self.assertEqual(plan_resource([], URL, PREFIX), ("create", None))

    def test_same_url_is_a_noop(self):
        items = [{"id": "7", "url": URL}]
        self.assertEqual(plan_resource(items, URL, PREFIX), ("noop", "7"))

    def test_stale_version_updates_in_place(self):
        # The whole point: a version bump must not leave a second row behind,
        # or the frontend keeps loading the old module from the old URL.
        items = [{"id": "7", "url": PREFIX + "?v=0.9.1"}]
        self.assertEqual(plan_resource(items, URL, PREFIX), ("update", "7"))

    def test_unversioned_row_also_updates_in_place(self):
        items = [{"id": "7", "url": PREFIX}]
        self.assertEqual(plan_resource(items, URL, PREFIX), ("update", "7"))

    def test_other_resources_are_never_touched(self):
        items = [
            {"id": "a", "url": "/hacsfiles/ha-floorplan/floorplan.js?hacstag=1"},
            {"id": "b", "url": "/hacsfiles/stack-in-card/stack-in-card.js?hacstag=2"},
        ]
        self.assertEqual(plan_resource(items, URL, PREFIX), ("create", None))

    def test_a_path_that_merely_starts_the_same_is_not_ours(self):
        items = [{"id": "x", "url": PREFIX + ".map?v=1"}]
        self.assertEqual(plan_resource(items, URL, PREFIX), ("create", None))

    def test_missing_or_null_url_is_skipped(self):
        items = [{"id": "x"}, {"id": "y", "url": None}, {"id": "z", "url": URL}]
        self.assertEqual(plan_resource(items, URL, PREFIX), ("noop", "z"))


if __name__ == "__main__":
    unittest.main()
