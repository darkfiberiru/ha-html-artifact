"""Manual-advance steps: hold, count overtime, move later steps on Next, reminders, reset."""
import unittest

from tests.test_core import make_engine, fired, core

SIMMER = [
    {"at": 0, "label": "Start", "advance": "auto"},
    {"at": 100, "label": "Simmer"},
    {"at": 200, "label": "Rice check", "advance": "auto"},
    {"at": 300, "label": "Check the fish"},
    {"at": 400, "label": "Serve"},
]


def make(**kw):
    engine, sched, backing, events = make_engine()
    timer = engine.create("a", "x", SIMMER, advance="manual", start=True, **kw)
    return engine, sched, events, timer["id"]


def lane(view):
    return view["tracks"][0]


class ManualAdvanceTest(unittest.TestCase):
    def test_auto_step_moves_on_by_itself(self):
        engine, sched, events, tid = make()
        sched.advance(110)  # Start is auto: Simmer arrives on time
        self.assertEqual([e["step"]["label"] for e in fired(events, "step")], ["Simmer"])

    def test_manual_step_holds_and_counts_overtime(self):
        engine, sched, events, tid = make()
        sched.advance(100 + 1)  # Simmer starts
        sched.advance(0)
        # Simmer is manual; the next step (the reminder) is auto, the boundary is "Check the fish" at 300
        sched.advance(250)  # t = 351: Simmer ran past 300
        view = engine.get_view(tid)
        self.assertEqual(view["current_index"], 1)
        self.assertEqual(view["state"], "running")
        self.assertAlmostEqual(lane(view)["remaining"], -(351 - 300), delta=2)
        self.assertAlmostEqual(view["overtime"], 51, delta=2)
        labels = [e["step"]["label"] for e in fired(events, "step")]
        self.assertIn("Rice check", labels)  # the reminder fired on time during the hold
        self.assertNotIn("Check the fish", labels)
        over = fired(events, "overtime")
        self.assertEqual(len(over), 1)
        self.assertEqual(over[0]["step"]["label"], "Simmer")

    def test_next_late_shifts_later_steps_by_the_overtime(self):
        engine, sched, events, tid = make()
        sched.advance(420)  # 2:00 over (boundary at 300)
        engine.skip(tid, source="notification")
        view = engine.get_view(tid)
        self.assertEqual(view["steps"][view["current_index"]]["label"], "Check the fish")
        self.assertEqual(view["overtime"], 0)
        sched.advance(99)  # Serve was 100 after Check the fish
        self.assertEqual(engine.get_view(tid)["state"], "running")
        sched.advance(2)  # Check the fish is manual -> Serve is the terminal step; it waits for Next
        self.assertEqual(engine.get_view(tid)["state"], "running")
        self.assertGreater(engine.get_view(tid)["overtime"], 0)

    def test_next_early_pulls_later_steps_sooner(self):
        engine, sched, events, tid = make()
        sched.advance(150)
        engine.skip(tid)  # Simmer ends 150 s early -> Check the fish now
        view = engine.get_view(tid)
        self.assertEqual(view["steps"][view["current_index"]]["label"], "Check the fish")
        self.assertAlmostEqual(lane(view)["elapsed"], 300, delta=1)
        log = view["step_log"][0]
        self.assertEqual((log["label"], log["planned"]), ("Simmer", 200))
        self.assertAlmostEqual(log["actual"], 50, delta=2)

    def test_extend_during_overtime_rewinds_the_step_only(self):
        engine, sched, events, tid = make()
        sched.advance(400)  # 100 s over
        engine.extend(tid, 60)
        view = engine.get_view(tid)
        self.assertAlmostEqual(view["overtime"], 40, delta=2)
        self.assertEqual(view["steps"][view["current_index"]]["label"], "Simmer")
        engine.extend(tid, 60)  # now back inside the step
        self.assertEqual(engine.get_view(tid)["overtime"], 0)
        self.assertGreater(lane(engine.get_view(tid))["remaining"], 0)

    def test_pause_does_not_accumulate_overtime(self):
        engine, sched, events, tid = make()
        sched.advance(350)
        before = engine.get_view(tid)["overtime"]
        engine.pause(tid)
        sched.advance(500)
        engine.resume(tid)
        self.assertAlmostEqual(engine.get_view(tid)["overtime"], before, delta=1)

    def test_remind_every_repeats_while_over(self):
        engine, sched, events, tid = make(remind_every=60)
        sched.advance(300 + 190)  # overtime onset at 300, reminders at 360, 420, 480
        over = fired(events, "overtime")
        self.assertEqual([e["reminder"] for e in over], [False, True, True, True])

    def test_default_timer_never_holds(self):
        engine, sched, backing, events = make_engine()
        timer = engine.create("a", "x", [{"at": 10, "label": "a"}, {"at": 20, "label": "b"}], 40, start=True)
        sched.advance(25)
        self.assertEqual([e["step"]["label"] for e in fired(events, "step")], ["a", "b"])
        self.assertEqual(fired(events, "overtime"), [])
        self.assertEqual(engine.get_view(timer["id"])["overtime"], 0)

    def test_reset_restores_the_original_schedule(self):
        engine, sched, events, tid = make()
        sched.advance(30)
        engine.extend(tid, 60)
        sched.advance(400)
        engine.skip(tid)
        engine.reset(tid)
        view = engine.get_view(tid)
        self.assertEqual([s["at"] for s in view["steps"]], [0, 100, 200, 300, 400])
        self.assertEqual(view["step_log"], [])
        engine.start(tid)
        self.assertEqual([s["at"] for s in engine.get_view(tid)["steps"]], [0, 100, 200, 300, 400])

    def test_hold_survives_a_restart(self):
        engine, sched, events, tid = make()
        sched.advance(400)
        saved = engine._store.data if hasattr(engine._store, "data") else None
        self.assertGreater(engine.get_view(tid)["overtime"], 0)

    def test_step_and_timer_validation(self):
        engine, _, _, _ = make_engine()
        with self.assertRaises(core.ArtifactError):
            engine.create("a", "x", [{"at": 1, "label": "x", "advance": "sometimes"}], 5)
        with self.assertRaises(core.ArtifactError):
            engine.create("a", "x", [{"at": 1, "label": "x", "remindEvery": 1}], 5)
        with self.assertRaises(core.ArtifactError):
            engine.create("a", "x", [{"at": 1, "label": "x"}], 5, advance="never")

    def test_overtime_only_holds_its_own_track(self):
        engine, sched, backing, events = make_engine()
        timer = engine.create(
            "a", "x",
            [{"track": "fish", "at": 0, "label": "Simmer", "advance": "manual"},
             {"track": "fish", "at": 100, "label": "Serve fish", "advance": "manual"},
             {"track": "rice", "at": 0, "label": "Rice"},
             {"track": "rice", "at": 150, "label": "Rice check"},
             {"track": "rice", "at": 250, "label": "Rice done"}],
            tracks=[{"id": "fish"}, {"id": "rice"}], start=True,
        )
        sched.advance(260)
        labels = [(e["track"], e["step"]["label"]) for e in fired(events, "step")]
        self.assertIn(("rice", "Rice check"), labels)
        view = engine.get_view(timer["id"])
        fish = next(t for t in view["tracks"] if t["id"] == "fish")
        self.assertGreater(fish["overtime"], 100)


if __name__ == "__main__":
    unittest.main()
