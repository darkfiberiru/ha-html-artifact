"""Regenerates tests/fixtures/lanes.json: real engine views of examples/cook-lanes.html's timer.

    python tests/make_lane_fixture.py
"""
import json
import pathlib
import re

from tests.test_core import make_engine

root = pathlib.Path(__file__).resolve().parents[1]
page = (root / "examples" / "cook-lanes.html").read_text()
MIN = 60
recipe = re.search(r"const RECIPE = (\{.*?\n\});", page, re.S).group(1)
# The recipe literal is plain JS; evaluate it with a tiny translation to JSON.
import subprocess
data = json.loads(subprocess.check_output(
    ["node", "-e", f"const MIN=60;const RECIPE={recipe};console.log(JSON.stringify(RECIPE))"]))

engine, sched, _, events = make_engine()
timer = engine.create("cook", data["label"], data["steps"], tracks=data["tracks"], start=True)
out = {"recipe": data, "created": timer}
sched.advance(300)
out["at_5min"] = engine.get_view(timer["id"])
sched.advance(1200)  # 25 min: the fish gate
out["at_gate"] = engine.get_view(timer["id"])
engine.answer(timer["id"], out["at_gate"]["waiting"][0]["index"], "yes")
sched.advance(300)
out["finished"] = engine.get_view(timer["id"])
(root / "tests" / "fixtures" / "lanes.json").write_text(json.dumps(out, indent=1))
print({k: out[k]["state"] for k in ("created", "at_5min", "at_gate", "finished")})
