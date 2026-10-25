"""Unit tests for the pure-Python core (no Home Assistant needed).

Run:  python -m unittest discover -s tests -v
"""
import asyncio
import copy
import importlib.util
import pathlib
import sys
import unittest

CORE = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "html_artifacts" / "core.py"
_spec = importlib.util.spec_from_file_location("ha_art_core", CORE)
core = importlib.util.module_from_spec(_spec)
sys.modules["ha_art_core"] = core
_spec.loader.exec_module(core)


class FakeStore:
    def __init__(self, data=None):
        self.data = data

    async def async_load(self):
        return copy.deepcopy(self.data)

    def async_delay_save(self, func, delay=0):
        self.data = copy.deepcopy(func())


class Sched:
    """Fake clock + scheduler: advance() runs callbacks in due order."""

    def __init__(self, now=1000.0):
        self.now = now
        self.jobs = []

    def clock(self):
        return self.now

    def schedule(self, delay, callback):
        job = [self.now + delay, callback, False]
        self.jobs.append(job)

        def cancel():
            job[2] = True

        return cancel

    def advance(self, seconds):
        end = self.now + seconds
        while True:
            due = sorted((j for j in self.jobs if not j[2] and j[0] <= end), key=lambda j: j[0])
            if not due:
                break
            job = due[0]
            job[2] = True
            self.now = max(self.now, job[0])
            job[1]()
        self.now = end


def make_store():
    clock = Sched()
    store = core.ArtifactStore(FakeStore(), clock.clock)
    return store, clock


def make_engine(sched=None, backing=None):
    sched = sched or Sched()
    backing = backing or FakeStore()
    engine = core.TimerEngine(backing, sched.clock, sched.schedule)
    events = []
    engine.add_listener(events.append)
    asyncio.run(engine.async_load())
    return engine, sched, backing, events


def fired(events, kind=None):
    return [e for e in events if e["type"] == "timer_fired" and (kind is None or e["kind"] == kind)]


STEPS = [
    {"at": 0, "label": "Start rice"},
    {"at": 300, "label": "Prep"},
    {"at": 720, "label": "Sear"},
    {"at": 1020, "label": "Top"},
    {"at": 1440, "label": "Serve"},
]


class GrantTests(unittest.TestCase):
    def test_patterns(self):
        grants = core.normalize_grants({"entities": ["Sensor.Oven_*", "light.kitchen"], "services": ["light.turn_*"]})
        self.assertTrue(core.entity_allowed(grants, "sensor.oven_temp"))
        self.assertTrue(core.entity_allowed(grants, "light.kitchen"))
        self.assertFalse(core.entity_allowed(grants, "light.bedroom"))
        self.assertTrue(core.service_allowed(grants, "light", "turn_on"))
        self.assertFalse(core.service_allowed(grants, "lock", "unlock"))

    def test_defaults_deny_everything(self):
        grants = core.empty_grants()
        self.assertFalse(core.entity_allowed(grants, "sensor.x"))
        self.assertFalse(grants["notify"])

    def test_rejects_garbage(self):
        with self.assertRaises(core.ArtifactError):
            core.normalize_grants({"entities": "sensor.x"})
        with self.assertRaises(core.ArtifactError):
            core.normalize_grants({"services": [1]})


class ArtifactTests(unittest.TestCase):
    def test_create_and_read(self):
        store, _ = make_store()
        art = store.save("recipes", "<p>hi</p>", title="Recipes", source="claude", updated_by="claude")
        self.assertEqual(art["version"], 1)
        self.assertEqual(store.get("recipes")["html"], "<p>hi</p>")
        self.assertEqual(store.list()[0]["id"], "recipes")
        self.assertEqual(art["caps_granted"], core.empty_grants())

    def test_agent_must_pass_base_version(self):
        store, _ = make_store()
        store.save("a", "v1", source="claude")
        with self.assertRaises(core.ArtifactError) as ctx:
            store.save("a", "v2", source="claude")
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertEqual(ctx.exception.extra["current"]["html"], "v1")

    def test_stale_agent_publish_is_rejected_after_hand_edit(self):
        store, _ = make_store()
        store.save("a", "v1", source="claude")
        store.save("a", "v1 edited by hand", base_version=1, source="user", updated_by="user:alex")
        with self.assertRaises(core.ArtifactError) as ctx:
            store.save("a", "claude rewrite", base_version=1, source="claude")
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertEqual(ctx.exception.extra["current"]["html"], "v1 edited by hand")
        self.assertEqual(ctx.exception.extra["current"]["updated_by"], "user:alex")
        merged = store.save("a", "merged", base_version=2, source="claude")
        self.assertEqual(merged["version"], 3)

    def test_unchanged_save_does_not_bump_version(self):
        store, _ = make_store()
        store.save("a", "same", source="claude")
        again = store.save("a", "same", base_version=1, source="claude")
        self.assertEqual(again["version"], 1)

    def test_history_cap_and_revert(self):
        store, _ = make_store()
        store.save("a", "0", source="claude")
        for i in range(1, 30):
            store.save("a", str(i), base_version=i, source="claude")
        hist = store.history("a")
        self.assertEqual(len(hist), core.MAX_HISTORY)
        self.assertEqual(hist[0]["version"], 29)
        reverted = store.revert("a", 25)
        self.assertEqual(reverted["html"], "24")  # v25 held html "24"
        self.assertEqual(reverted["version"], 31)

    def test_validation(self):
        store, _ = make_store()
        for bad in ("Bad Id", "-x", "", "a" * 70):
            with self.assertRaises(core.ArtifactError):
                store.save(bad, "x")
        with self.assertRaises(core.ArtifactError) as ctx:
            store.save("big", "x" * (core.MAX_HTML_BYTES + 1))
        self.assertEqual(ctx.exception.code, "too_large")

    def test_grants_are_user_controlled_and_survive_republish(self):
        store, _ = make_store()
        store.save("a", "v1", source="claude", caps_requested={"notify": True, "entities": ["sensor.*"]})
        self.assertFalse(store.get_grants("a")["notify"])
        store.set_grants("a", {"notify": True})
        store.save("a", "v2", base_version=1, source="claude", caps_requested={"notify": True, "entities": ["sensor.*", "light.*"]})
        self.assertTrue(store.get_grants("a")["notify"])
        self.assertEqual(store.get_grants("a")["entities"], [])

    def test_delete_removes_state(self):
        store, _ = make_store()
        store.save("a", "x")
        store.kv_set("a", "k", 1)
        store.delete("a")
        with self.assertRaises(core.ArtifactError):
            store.get("a")
        self.assertEqual(store._data["kv"], {})

    def test_persistence_roundtrip(self):
        backing = FakeStore()
        clock = Sched()
        s1 = core.ArtifactStore(backing, clock.clock)
        s1.save("a", "hello", title="T")
        s1.kv_set("a", "checked", [1, 2])
        s2 = core.ArtifactStore(backing, clock.clock)
        asyncio.run(s2.async_load())
        self.assertEqual(s2.get("a")["title"], "T")
        self.assertEqual(s2.kv_get("a", "checked"), [1, 2])


class KvTests(unittest.TestCase):
    def test_set_get_delete_and_events(self):
        store, _ = make_store()
        events = []
        store.add_listener(events.append)
        store.save("a", "x")
        store.kv_set("a", "n", {"x": 1})
        self.assertEqual(store.kv_get("a", "n"), {"x": 1})
        self.assertIsNone(store.kv_get("a", "missing"))
        store.kv_delete("a", "n")
        kv_events = [e for e in events if e["type"] == "kv"]
        self.assertEqual([e["deleted"] for e in kv_events], [False, True])

    def test_limits(self):
        store, _ = make_store()
        store.save("a", "x")
        with self.assertRaises(core.ArtifactError):
            store.kv_set("a", "k", "x" * (core.MAX_KV_VALUE_BYTES + 1))
        with self.assertRaises(core.ArtifactError):
            store.kv_set("a", "k", object())
        for i in range(core.MAX_KV_KEYS):
            store.kv_set("a", f"k{i}", i)
        with self.assertRaises(core.ArtifactError) as ctx:
            store.kv_set("a", "one-too-many", 1)
        self.assertEqual(ctx.exception.code, "limit")
        store.kv_set("a", "k0", "overwrite is fine")

    def test_requires_existing_artifact(self):
        store, _ = make_store()
        with self.assertRaises(core.ArtifactError):
            store.kv_set("nope", "k", 1)


class TimerTests(unittest.TestCase):
    def test_full_run_with_pause(self):
        engine, sched, _, events = make_engine()
        timer = engine.create("a", "Dinner", STEPS, 1500, start=True)
        tid = timer["id"]
        self.assertEqual(fired(events), [])  # step at 0 is not announced

        sched.advance(299)
        self.assertEqual(fired(events), [])
        sched.advance(2)  # t=301
        self.assertEqual([e["step"]["label"] for e in fired(events, "step")], ["Prep"])
        view = engine.get_view(tid)
        self.assertEqual((view["current_index"], view["next_index"]), (1, 2))
        self.assertAlmostEqual(view["next_in"], 419, delta=1)

        sched.advance(99)  # t=400
        engine.pause(tid)
        sched.advance(10_000)
        self.assertEqual(len(fired(events)), 1)  # nothing fires while paused
        self.assertAlmostEqual(engine.get_view(tid)["elapsed_now"], 400, delta=1)

        engine.resume(tid)
        sched.advance(321)  # elapsed 721
        self.assertEqual([e["step"]["label"] for e in fired(events, "step")], ["Prep", "Sear"])

        sched.advance(2000)
        labels = [e["step"]["label"] for e in fired(events, "step")]
        self.assertEqual(labels, ["Prep", "Sear", "Top", "Serve"])
        self.assertEqual(len(fired(events, "finished")), 1)
        self.assertEqual(engine.get_view(tid)["state"], "finished")

    def test_countdown_without_steps(self):
        engine, sched, _, events = make_engine()
        timer = engine.create("a", "Eggs", None, 60, start=True)
        sched.advance(59)
        self.assertEqual(fired(events), [])
        sched.advance(2)
        self.assertEqual(len(fired(events, "finished")), 1)
        self.assertEqual(engine.get_view(timer["id"])["state"], "finished")

    def test_step_at_the_very_end_is_folded_into_finish(self):
        engine, sched, _, events = make_engine()
        engine.create("a", "x", [{"at": 60, "label": "Serve"}], start=True)
        sched.advance(61)
        self.assertEqual(len(fired(events, "step")), 0)
        finished = fired(events, "finished")
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["step"]["label"], "Serve")

    def test_notify_false_steps_are_silent(self):
        engine, sched, _, events = make_engine()
        engine.create("a", "x", [{"at": 10, "label": "quiet", "notify": False}], 30, start=True)
        sched.advance(15)
        self.assertEqual(fired(events, "step"), [])

    def test_extend_shifts_remaining_steps(self):
        engine, sched, _, events = make_engine()
        timer = engine.create("a", "x", [{"at": 100, "label": "one"}, {"at": 200, "label": "two"}], 300, start=True)
        sched.advance(50)
        view = engine.extend(timer["id"], 60)
        self.assertEqual([s["at"] for s in view["steps"]], [160, 260])
        self.assertEqual(view["duration"], 360)
        sched.advance(70)  # elapsed 120: old time for step one, but it moved
        self.assertEqual(fired(events, "step"), [])
        sched.advance(41)  # elapsed 161
        self.assertEqual([e["step"]["label"] for e in fired(events, "step")], ["one"])

    def test_skip_and_reset(self):
        engine, sched, _, events = make_engine()
        timer = engine.create("a", "x", STEPS, 1500, start=True)
        tid = timer["id"]
        view = engine.skip(tid)
        self.assertEqual(view["current_index"], 1)  # jumped to Prep at 300
        self.assertEqual(fired(events), [])  # skipping does not alert
        view = engine.skip(tid, 3)
        self.assertEqual(view["current_index"], 3)
        sched.advance(100)
        self.assertEqual([e["step"]["label"] for e in fired(events, "step")], [])  # Top at 1020 reached silently
        sched.advance(330)  # elapsed 1450 -> Serve at 1440
        self.assertEqual([e["step"]["label"] for e in fired(events, "step")], ["Serve"])
        engine.reset(tid)
        self.assertEqual(engine.get_view(tid)["state"], "idle")
        engine.start(tid)  # can run again after a reset
        self.assertEqual(engine.get_view(tid)["state"], "running")

    def test_invalid_transitions_and_ownership(self):
        engine, _, _, _ = make_engine()
        timer = engine.create("a", "x", None, 60)
        tid = timer["id"]
        with self.assertRaises(core.ArtifactError):
            engine.pause(tid)
        engine.start(tid)
        with self.assertRaises(core.ArtifactError):
            engine.start(tid)
        with self.assertRaises(core.ArtifactError):
            engine.control(tid, "pause", artifact_id="someone-else")
        with self.assertRaises(core.ArtifactError):
            engine.create("a", "bad", [{"at": 100, "label": "x"}], 50)

    def test_restart_mid_timer_rearms(self):
        sched = Sched()
        backing = FakeStore()
        engine, _, _, _ = make_engine(sched, backing)
        engine.create("a", "x", [{"at": 300, "label": "one"}], 600, start=True)
        sched.advance(100)

        # "restart": a brand-new engine loading the persisted state
        backing2 = FakeStore(copy.deepcopy(backing.data))
        engine2, _, _, events2 = make_engine(sched, backing2)
        sched.advance(150)
        self.assertEqual(fired(events2), [])
        sched.advance(60)  # elapsed 310
        self.assertEqual([e["step"]["label"] for e in fired(events2, "step")], ["one"])

    def test_downtime_catch_up_announces_only_latest_step(self):
        sched = Sched()
        backing = FakeStore()
        engine, _, _, _ = make_engine(sched, backing)
        engine.create("a", "x", [{"at": 300, "label": "one"}, {"at": 720, "label": "two"}], 1440, start=True)

        later = Sched(now=sched.now + 800)  # HA was down for 800s
        engine2, _, _, events2 = make_engine(later, FakeStore(copy.deepcopy(backing.data)))
        later.advance(0)
        steps = fired(events2, "step")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["step"]["label"], "two")
        self.assertEqual(steps[0]["missed"], 1)

    def test_downtime_past_the_end_just_finishes(self):
        sched = Sched()
        backing = FakeStore()
        engine, _, _, _ = make_engine(sched, backing)
        engine.create("a", "x", [{"at": 300, "label": "one"}], 600, start=True)
        later = Sched(now=sched.now + 5000)
        _, _, _, events2 = make_engine(later, FakeStore(copy.deepcopy(backing.data)))
        later.advance(0)
        self.assertEqual(fired(events2, "step"), [])
        self.assertEqual(len(fired(events2, "finished")), 1)

    def test_delete_for_artifact(self):
        engine, sched, _, events = make_engine()
        engine.create("a", "one", None, 60, start=True)
        engine.create("b", "two", None, 60, start=True)
        engine.delete_for_artifact("a")
        self.assertEqual([t["artifact_id"] for t in engine.list()], ["b"])
        sched.advance(100)
        self.assertEqual(len(fired(events, "finished")), 1)

    def test_timer_limit(self):
        engine, _, _, _ = make_engine()
        for _ in range(core.MAX_TIMERS):
            engine.create("a", "t", None, 60)
        with self.assertRaises(core.ArtifactError):
            engine.create("a", "one too many", None, 60)


if __name__ == "__main__":
    unittest.main()
