"""Tracks, step durations and check gates in the timer engine."""
import unittest

from tests.test_core import Sched, make_engine, fired, core

TIKKA = {
    "tracks": [{"id": "rice", "label": "Rice"}, {"id": "fish", "label": "Fish"}],
    "steps": [
        {"track": "rice", "at": 0, "label": "Start the rice"},
        {"track": "rice", "at": 960, "label": "Steaming"},
        {"track": "rice", "at": 1560, "label": "Rice ready"},
        {"track": "fish", "at": 420, "label": "Heat the sauce"},
        {"track": "fish", "at": 600, "label": "Add frozen fish"},
        {"track": "fish", "at": 1080, "label": "Spoon sauce over"},
        {"track": "fish", "at": 1500, "label": "Check the fish",
         "gate": {"question": "145F?", "yes": "Done", "more": [60, 120]}},
        {"track": "*", "label": "Serve"},
    ],
}


def tikka(engine, **kw):
    return engine.create("a", "Tikka", TIKKA["steps"], tracks=TIKKA["tracks"], start=True, **kw)


def track(view, tid):
    return next(t for t in view["tracks"] if t["id"] == tid)


class TracksTest(unittest.TestCase):
    def test_flat_timer_has_one_implicit_track(self):
        engine, _, _, _ = make_engine()
        view = engine.create("a", "x", [{"at": 10, "label": "a"}], 30)
        self.assertFalse(view["multi"])
        self.assertEqual([t["id"] for t in view["tracks"]], ["main"])

    def test_duration_is_longest_lane(self):
        engine, _, _, _ = make_engine()
        view = engine.create("a", "x", TIKKA["steps"], tracks=TIKKA["tracks"])
        self.assertEqual(view["duration"], 1560)  # fish gate at 1500, rice ends 1560
        self.assertTrue(view["multi"])

    def test_tracks_fire_independently_and_name_their_track(self):
        engine, sched, _, events = make_engine()
        tikka(engine)
        sched.advance(1000)
        got = [(e["track"], e["step"]["label"]) for e in fired(events, "step")]
        self.assertEqual(got, [("fish", "Heat the sauce"), ("fish", "Add frozen fish"), ("rice", "Steaming")])

    def test_extend_one_track_leaves_the_other(self):
        engine, sched, _, events = make_engine()
        timer = tikka(engine)
        sched.advance(700)
        view = engine.extend(timer["id"], 120, track="fish")
        by = {s["label"]: s["at"] for s in view["steps"]}
        self.assertEqual(by["Spoon sauce over"], 1200)
        self.assertEqual(by["Steaming"], 960)
        self.assertEqual(track(view, "fish")["offset"], 120)
        self.assertEqual(track(view, "rice")["offset"], 0)

    def test_pause_one_track_only(self):
        engine, sched, _, events = make_engine()
        timer = tikka(engine)
        sched.advance(500)
        engine.pause(timer["id"], track="fish")
        sched.advance(600)  # rice keeps going, fish frozen at 500
        labels = [e["step"]["label"] for e in fired(events, "step")]
        self.assertIn("Steaming", labels)
        self.assertNotIn("Add frozen fish", labels)
        view = engine.resume(timer["id"], track="fish")
        self.assertEqual(track(view, "fish")["state"], "running")
        sched.advance(101)
        self.assertIn("Add frozen fish", [e["step"]["label"] for e in fired(events, "step")])

    def test_skip_one_track(self):
        engine, sched, _, events = make_engine()
        timer = tikka(engine)
        view = engine.skip(timer["id"], track="fish")
        self.assertEqual(track(view, "fish")["current_index"], track(view, "fish")["current_index"])
        self.assertEqual(fired(events, "step"), [])
        self.assertEqual(track(view, "fish")["elapsed"], 420)
        self.assertEqual(track(view, "rice")["elapsed"], 0)

    def test_gate_pauses_its_lane_and_waits(self):
        engine, sched, _, events = make_engine()
        timer = tikka(engine)
        sched.advance(1500)
        gates = fired(events, "gate")
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0]["track"], "fish")
        view = engine.get_view(timer["id"])
        self.assertEqual(track(view, "fish")["state"], "waiting")
        self.assertEqual(view["waiting"], [{"track": "fish", "index": gates[0]["index"]}])
        sched.advance(300)  # rice finishes meanwhile; Serve must NOT fire (fish not done)
        self.assertEqual(fired(events, "finished"), [])
        self.assertEqual(engine.get_view(timer["id"])["state"], "running")

    def test_gate_yes_then_join_finishes_with_serve(self):
        engine, sched, _, events = make_engine()
        timer = tikka(engine)
        sched.advance(1600)
        index = fired(events, "gate")[0]["index"]
        engine.answer(timer["id"], index, "yes")
        finished = fired(events, "finished")
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["step"]["label"], "Serve")
        self.assertEqual(engine.get_view(timer["id"])["state"], "finished")

    def test_gate_more_time_refires(self):
        engine, sched, _, events = make_engine()
        timer = tikka(engine)
        sched.advance(1500)
        index = fired(events, "gate")[0]["index"]
        engine.answer(timer["id"], index, 120)
        view = engine.get_view(timer["id"])
        self.assertEqual(track(view, "fish")["state"], "running")
        self.assertEqual(track(view, "fish")["offset"], 120)
        sched.advance(119)
        self.assertEqual(len(fired(events, "gate")), 1)
        sched.advance(2)
        self.assertEqual(len(fired(events, "gate")), 2)  # asked again
        engine.answer(timer["id"], index, "yes")
        sched.advance(200)
        self.assertEqual(len(fired(events, "finished")), 1)

    def test_answer_requires_a_waiting_gate(self):
        engine, sched, _, events = make_engine()
        timer = tikka(engine)
        with self.assertRaises(core.ArtifactError):
            engine.answer(timer["id"], 6, "yes")

    def test_auto_yes_after(self):
        engine, sched, _, events = make_engine()
        timer = engine.create(
            "a", "x",
            [{"track": "t", "at": 10, "label": "Check", "gate": {"question": "ok?", "autoYesAfter": 30}}],
            start=True,
        )
        sched.advance(11)
        self.assertEqual(len(fired(events, "gate")), 1)
        self.assertEqual(engine.get_view(timer["id"])["state"], "running")
        sched.advance(31)
        self.assertEqual(engine.get_view(timer["id"])["state"], "finished")

    def test_activity_remaining_and_end_label(self):
        engine, sched, _, events = make_engine()
        timer = engine.create(
            "a", "x",
            [{"track": "fish", "at": 10, "label": "Simmer", "duration": 100, "endLabel": "Simmer done"},
             {"track": "fish", "at": 130, "label": "Serve"}],
            start=True,
        )
        sched.advance(40)
        t = track(engine.get_view(timer["id"]), "fish")
        self.assertEqual(t["current_index"], 0)
        self.assertAlmostEqual(t["remaining"], 70, delta=1)
        sched.advance(75)
        ends = [e for e in fired(events, "step") if e["phase"] == "end"]
        self.assertEqual([e["step"]["endLabel"] for e in ends], ["Simmer done"])

    def test_extend_stretches_the_running_activity(self):
        engine, sched, _, events = make_engine()
        timer = engine.create(
            "a", "x", [{"track": "fish", "at": 10, "label": "Simmer", "duration": 100}], start=True)
        sched.advance(40)
        view = engine.extend(timer["id"], 60, track="fish")
        self.assertEqual(view["steps"][0]["duration"], 160)
        self.assertEqual(view["duration"], 170)

    def test_old_saved_timers_migrate(self):
        engine, sched, backing, events = make_engine()
        old = {
            "id": "old1", "artifact_id": "a", "label": "legacy", "state": "paused", "elapsed": 20.0,
            "started_at": None, "fired": 1, "created": 0, "finished_at": None, "duration": 60,
            "notify_targets": [],
            "steps": [{"at": 10, "label": "a", "detail": "", "notify": True},
                      {"at": 40, "label": "b", "detail": "", "notify": True}],
        }
        core.TimerEngine._migrate(old)
        engine._timers["old1"] = old
        view = engine.get_view("old1")
        self.assertEqual(view["current_index"], 0)
        self.assertEqual(view["next_index"], 1)
        engine.resume("old1")
        sched.advance(25)
        self.assertEqual([e["step"]["label"] for e in fired(events, "step")], ["b"])

    def test_history_records_actions(self):
        engine, sched, _, events = make_engine()
        timer = tikka(engine)
        sched.advance(10)
        engine.extend(timer["id"], 60, track="fish", source="notification")
        view = engine.get_view(timer["id"])
        actions = [(h["action"], h.get("track"), h.get("source")) for h in view["history"]]
        self.assertIn(("extend", "fish", "notification"), actions)

    def test_validation(self):
        engine, _, _, _ = make_engine()
        with self.assertRaises(core.ArtifactError):
            engine.create("a", "x", [{"track": "bad id!", "at": 1, "label": "x"}])
        with self.assertRaises(core.ArtifactError):
            engine.create("a", "x", [{"track": "a", "at": 1, "label": "x"}, {"after": ["zzz"], "label": "j"}])
        with self.assertRaises(core.ArtifactError):
            engine.create("a", "x", [{"track": "a", "at": 1, "label": "x", "gate": {"more": [0]}}])


if __name__ == "__main__":
    unittest.main()
