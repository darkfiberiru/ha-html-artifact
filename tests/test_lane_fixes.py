"""Lane bugs found while testing recipe pages: Next vs gates, a manual last step, step_log, join."""
import unittest

from tests.test_core import make_engine, fired

GATE = {"question": "145F?", "yes": "Done", "more": [60, 120]}


def fish(extra=None, tracks=None):
    steps = [
        {"track": "fish", "at": 0, "label": "Prep", "advance": "auto"},
        {"track": "fish", "at": 660, "label": "Simmer", "advance": "manual", "duration": 840},
        {"track": "fish", "at": 1500, "label": "Check the fish", "gate": GATE},
        {"track": "*", "label": "Serve"},
    ]
    return steps


def lane(view, tid):
    return next(t for t in view["tracks"] if t["id"] == tid)


class NextBeforeGateTest(unittest.TestCase):
    def check(self, gate_at):
        engine, sched, _, events = make_engine()
        steps = fish()
        steps[2]["at"] = gate_at
        timer = engine.create("a", "x", steps, tracks=[{"id": "fish"}], start=True)
        tid = timer["id"]
        sched.advance(1540)  # Simmer's time is up at 1500: 40 s over
        self.assertGreater(lane(engine.get_view(tid), "fish")["overtime"], 30)
        engine.skip(tid, track="fish")
        view = engine.get_view(tid)
        self.assertEqual(lane(view, "fish")["state"], "waiting")
        self.assertEqual(lane(view, "fish")["waiting"], 2)
        self.assertEqual(view["state"], "running")
        self.assertEqual(len(fired(events, "gate")), 1)
        engine.answer(tid, 2, "yes")
        self.assertEqual(engine.get_view(tid)["state"], "finished")

    def test_next_lands_on_the_gate(self):
        self.check(1500)

    def test_next_lands_on_a_gate_one_second_later(self):
        self.check(1501)

    def test_next_early_still_asks(self):
        engine, sched, _, events = make_engine()
        timer = engine.create("a", "x", fish(), tracks=[{"id": "fish"}], start=True)
        sched.advance(900)
        engine.skip(timer["id"], track="fish")
        self.assertEqual(lane(engine.get_view(timer["id"]), "fish")["waiting"], 2)
        self.assertEqual(len(fired(events, "gate")), 1)


class LastManualStepTest(unittest.TestCase):
    def make(self, marker=False):
        engine, sched, _, events = make_engine()
        steps = [
            {"at": 0, "label": "Bake", "advance": "auto", "duration": 1500},
            {"at": 1560, "label": "Lemon butter", "advance": "manual", "duration": 120},
        ]
        if marker:
            steps.append({"at": 1680, "label": "Ready to plate", "advance": "auto"})
        timer = engine.create("a", "x", steps, start=True)
        return engine, sched, events, timer["id"]

    def test_last_manual_step_holds_in_overtime(self):
        engine, sched, events, tid = self.make()
        sched.advance(1560 + 120 + 60)
        view = engine.get_view(tid)
        self.assertEqual(view["state"], "running")
        self.assertAlmostEqual(lane(view, "main")["remaining"], -60, delta=2)
        self.assertAlmostEqual(view["overtime"], 60, delta=2)
        self.assertEqual(len(fired(events, "overtime")), 1)
        self.assertEqual(fired(events, "finished"), [])

    def test_next_ends_it_and_is_logged(self):
        engine, sched, events, tid = self.make()
        sched.advance(1560 + 120 + 60)
        engine.skip(tid)
        view = engine.get_view(tid)
        self.assertEqual(view["state"], "finished")
        log = [r for r in view["step_log"] if r["label"] == "Lemon butter"]
        self.assertEqual(len(log), 1)
        self.assertAlmostEqual(log[0]["actual"], 180, delta=2)
        self.assertEqual(log[0]["planned"], 120)

    def test_holds_before_the_planned_end_too(self):
        engine, sched, events, tid = self.make()
        sched.advance(1560 + 60)
        engine.skip(tid)
        self.assertEqual(engine.get_view(tid)["state"], "finished")

    def test_extend_in_overtime(self):
        engine, sched, events, tid = self.make()
        sched.advance(1560 + 120 + 30)
        engine.extend(tid, 60)
        view = engine.get_view(tid)
        self.assertEqual(view["state"], "running")
        self.assertEqual(view["overtime"], 0)

    def test_in_a_lane_with_other_lanes(self):
        engine, sched, _, events = make_engine()
        steps = [
            {"track": "a", "at": 0, "label": "A1", "advance": "auto", "duration": 100},
            {"track": "a", "at": 100, "label": "A2", "advance": "manual", "duration": 50},
            {"track": "b", "at": 0, "label": "B1", "advance": "auto", "duration": 400},
        ]
        timer = engine.create("a", "x", steps, tracks=[{"id": "a"}, {"id": "b"}], start=True)
        tid = timer["id"]
        sched.advance(200)
        view = engine.get_view(tid)
        self.assertEqual(lane(view, "a")["state"], "running")
        self.assertGreater(lane(view, "a")["overtime"], 40)
        engine.skip(tid, track="a")
        self.assertEqual(lane(engine.get_view(tid), "a")["state"], "done")
        self.assertEqual(engine.get_view(tid)["state"], "running")  # B still going
        sched.advance(250)
        self.assertEqual(engine.get_view(tid)["state"], "finished")

    def test_join_waits_for_the_held_lane(self):
        engine, sched, _, events = make_engine()
        steps = [
            {"track": "a", "at": 0, "label": "A1", "advance": "auto", "duration": 100},
            {"track": "a", "at": 100, "label": "A2", "advance": "manual", "duration": 50},
            {"track": "b", "at": 0, "label": "B1", "advance": "auto", "duration": 120},
            {"track": "*", "label": "Serve", "after": ["a", "b"]},
        ]
        timer = engine.create("a", "x", steps, tracks=[{"id": "a"}, {"id": "b"}], start=True)
        tid = timer["id"]
        sched.advance(400)
        view = engine.get_view(tid)
        self.assertEqual(view["state"], "running")  # Serve must not fire while A2 waits for Next
        self.assertNotIn("Serve", [e["step"]["label"] for e in fired(events, "step")])
        engine.skip(tid, track="a")
        self.assertEqual(engine.get_view(tid)["state"], "finished")

    def test_mid_lane_manual_hold_blocks_the_join(self):
        engine, sched, _, events = make_engine()
        steps = [
            {"track": "a", "at": 0, "label": "A1", "advance": "manual"},
            {"track": "a", "at": 100, "label": "A2", "advance": "auto", "duration": 50},
            {"track": "*", "label": "Serve", "after": ["a"]},
        ]
        timer = engine.create("a", "x", steps, tracks=[{"id": "a"}], start=True)
        tid = timer["id"]
        sched.advance(400)
        self.assertEqual(engine.get_view(tid)["state"], "running")
        self.assertNotIn("Serve", [e["step"]["label"] for e in fired(events, "step")])


class StepLogTest(unittest.TestCase):
    def test_next_before_a_terminal_marker_is_logged(self):
        engine, sched, _, events = make_engine()
        steps = [
            {"at": 0, "label": "Start", "advance": "auto"},
            {"at": 100, "label": "Butter", "advance": "manual"},
            {"at": 200, "label": "Ready to plate", "advance": "auto"},
        ]
        timer = engine.create("a", "x", steps, start=True)
        sched.advance(260)
        engine.skip(timer["id"])
        view = engine.get_view(timer["id"])
        self.assertEqual(view["state"], "finished")
        self.assertEqual([r["label"] for r in view["step_log"]], ["Butter"])
        self.assertAlmostEqual(view["step_log"][0]["actual"], 160, delta=2)


class ExtraTimeTest(unittest.TestCase):
    def test_gate_answers_and_extends_are_summed_per_step(self):
        engine, sched, _, events = make_engine()
        steps = [
            {"track": "fish", "at": 0, "label": "Simmer", "advance": "auto", "duration": 100},
            {"track": "fish", "at": 100, "label": "Check", "gate": GATE},
        ]
        timer = engine.create("a", "x", steps, tracks=[{"id": "fish"}], start=True)
        tid = timer["id"]
        sched.advance(50)
        engine.extend(tid, 30, track="fish")  # +30 on Simmer
        sched.advance(90)  # gate at 130
        engine.answer(tid, 1, 60)
        sched.advance(70)  # gate again at 190
        engine.answer(tid, 1, 120)
        rows = {r["index"]: r for r in engine.get_view(tid)["extra"]}
        self.assertEqual(rows[0]["extend"], 30)
        self.assertEqual(rows[0]["label"], "Simmer")
        self.assertEqual(rows[1]["gate"], 180)
        self.assertEqual(rows[1]["seconds"], 180)

    def test_reset_clears_it(self):
        engine, sched, _, events = make_engine()
        timer = engine.create("a", "x", [{"at": 0, "label": "A", "duration": 50}, {"at": 50, "label": "B"}], start=True)
        sched.advance(10)
        engine.extend(timer["id"], 20)
        self.assertTrue(engine.get_view(timer["id"])["extra"])
        engine.reset(timer["id"])
        self.assertEqual(engine.get_view(timer["id"])["extra"], [])


if __name__ == "__main__":
    unittest.main()
