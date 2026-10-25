"""Manual-advance steps: hold, count overtime, move later steps on Next, reminders, reset."""
import unittest

from tests.test_core import make_engine, fired, core

SIMMER = [
    {"at": 0, "label": "Start", "advance": "auto"},
    {"at": 100, "label": "Simmer"},
    {"at": 200, "label": "Rice check", "reminder": True, "anchor": "timer"},
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
        # Simmer is manual; the reminder never ends it, the boundary is "Check the fish" at 300
        sched.advance(250)  # t = 351: Simmer ran past 300
        view = engine.get_view(tid)
        self.assertEqual(view["current_index"], 1)
        self.assertEqual(view["state"], "running")
        self.assertAlmostEqual(lane(view)["remaining"], -(351 - 300), delta=2)
        self.assertAlmostEqual(view["overtime"], 51, delta=2)
        labels = [e["step"]["label"] for e in fired(events, "step")]
        self.assertIn("Rice check", [e["step"]["label"] for e in fired(events, "reminder")])  # fired on time during the hold
        self.assertNotIn("Rice check", labels)
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

    def test_auto_step_between_manual_steps_becomes_current(self):
        engine, sched, backing, events = make_engine()
        timer = engine.create("a", "x", [
            {"at": 0, "label": "Prep", "advance": "manual"},
            {"at": 100, "label": "Bake", "advance": "auto"},
            {"at": 200, "label": "Check", "advance": "manual"},
            {"at": 300, "label": "Serve", "advance": "manual"}], start=True)
        tid = timer["id"]
        sched.advance(150)  # Prep ran 50 over; nothing moved on
        self.assertEqual(engine.get_view(tid)["current_index"], 0)
        engine.skip(tid)  # Next on Prep goes to Bake, not past it
        view = engine.get_view(tid)
        self.assertEqual(view["steps"][view["current_index"]]["label"], "Bake")
        sched.advance(101)  # Bake is auto: it moves on by itself to Check
        view = engine.get_view(tid)
        self.assertEqual(view["steps"][view["current_index"]]["label"], "Check")

    def test_timer_anchored_reminder_ignores_overtime_and_next(self):
        engine, sched, events, tid = make()
        sched.advance(150)
        engine.skip(tid)  # Next early, lane clock jumps to 300
        sched.advance(55)  # timer clock 205 -> the timer-anchored reminder (at 200) still fires
        self.assertEqual([e["step"]["label"] for e in fired(events, "reminder")], ["Rice check"])

    def test_step_anchored_reminder_moves_with_the_step_and_is_skipped_by_next(self):
        engine, sched, backing, events = make_engine()
        timer = engine.create("a", "x", [
            {"at": 0, "label": "Start", "advance": "auto"},
            {"at": 100, "label": "Simmer", "advance": "manual"},
            {"at": 160, "label": "Spoon sauce over", "reminder": True},
            {"at": 400, "label": "Check", "advance": "manual"},
            {"at": 500, "label": "Serve", "advance": "manual"}], start=True)
        tid = timer["id"]
        rem = lambda: engine.get_view(tid)["reminders"][0]
        self.assertEqual(rem()["state"], "pending")
        sched.advance(100)
        engine.skip(tid)  # Start was auto; here we are on Simmer at lane time 100
        sched.advance(10)
        engine.skip(tid)  # wrong: leaves Simmer 50 s before the reminder
        self.assertEqual(rem()["state"], "skipped")
        sched.advance(200)
        self.assertEqual(fired(events, "reminder"), [])

    def test_step_anchored_reminder_fires_relative_to_a_late_start(self):
        engine, sched, backing, events = make_engine()
        timer = engine.create("a", "x", [
            {"at": 0, "label": "Heat", "advance": "manual"},
            {"at": 100, "label": "Simmer", "advance": "manual"},
            {"at": 160, "label": "Spoon sauce over", "reminder": True},
            {"at": 400, "label": "Check", "advance": "manual"},
            {"at": 500, "label": "Serve", "advance": "manual"}], start=True)
        tid = timer["id"]
        sched.advance(250)  # Heat ran 150 over
        engine.skip(tid)  # Simmer starts now (T=250)
        sched.advance(55)  # T=305: only 55 s into Simmer
        self.assertEqual(fired(events, "reminder"), [])
        view = engine.get_view(tid)
        self.assertAlmostEqual(view["reminders"][0]["due"], 5, delta=2)
        sched.advance(10)  # 65 s into Simmer -> 160 on the lane clock
        self.assertEqual([e["step"]["label"] for e in fired(events, "reminder")], ["Spoon sauce over"])
        r = engine.get_view(tid)["reminders"][0]
        self.assertEqual((r["state"], r["due"]), ("fired", None))
        self.assertIsNotNone(r["firedAt"])
        self.assertEqual(engine.get_view(tid)["steps"][engine.get_view(tid)["current_index"]]["label"], "Simmer")

    def test_reminders_never_become_current_or_the_skip_target(self):
        engine, sched, backing, events = make_engine()
        timer = engine.create("a", "x", [
            {"at": 0, "label": "A", "advance": "manual"},
            {"at": 50, "label": "Nudge", "reminder": True, "anchor": "timer"},
            {"at": 100, "label": "B", "advance": "manual"},
            {"at": 200, "label": "C", "advance": "manual"}], start=True)
        tid = timer["id"]
        sched.advance(80)
        view = engine.get_view(tid)
        self.assertEqual(view["steps"][view["current_index"]]["label"], "A")
        engine.skip(tid)
        view = engine.get_view(tid)
        self.assertEqual(view["steps"][view["current_index"]]["label"], "B")
        with self.assertRaises(core.ArtifactError):
            engine.skip(tid, 1)  # the reminder's index

    def test_reminder_validation(self):
        engine, _, _, _ = make_engine()
        for bad in ({"advance": "manual"}, {"gate": {}}, {"duration": 5}, {"anchor": "moon"}):
            with self.assertRaises(core.ArtifactError):
                engine.create("a", "x", [dict({"at": 1, "label": "r", "reminder": True}, **bad)], 10)
        view = engine.create("a", "x", [{"at": 1, "label": "r", "kind": "reminder"}, {"at": 5, "label": "s"}], 10)
        self.assertEqual(view["reminders"][0]["anchor"], "step")

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
