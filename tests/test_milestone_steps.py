"""A milestone with unticked steps in the plan is never done, even when every requirement of its written specs
passes (seen on 28 October 2026: M0 had 3 of 5 steps ticked and `milestone M0` said it was done).
Run: python -m unittest tests.test_milestone_steps -v"""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_regista import E2E_GATE, Project, as_agent, decision, regista  # noqa: E402

MEAN_SPEC = {"step": "mean", "milestone": "M0", "title": "Mean", "requirements": [
    {"id": "MEAN-1", "text": "WHEN the list is empty mean SHALL raise ValueError", "verify": "test"},
    {"id": "MEAN-2", "text": "WHEN run from the CLI the tool SHALL print the mean", "verify": "e2e"}]}

# Three of five steps ticked under M0, like the case that was reported; M1 keeps a build-all run going.
PLAN_OPEN = """# Demo: plan

## M0 — Basics
Steps:
- [x] mean
- [x] median
- [x] mode
- [ ] range
- [ ] spread

## M1 — CLI
Steps:
- [ ] calc command
"""

PLAN_TICKED = PLAN_OPEN.replace("- [ ] range", "- [x] range").replace("- [ ] spread", "- [x] spread")


class MilestoneOpenSteps(unittest.TestCase):
    def make(self, plan):
        """A project whose M0 spec requirements all pass, through a commit the gates allowed."""
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}, E2E_GATE])
        self.p.commit(".regista/plan.md", plan, "docs: plan")
        self.p.write(".regista/specs/mean.json", json.dumps(MEAN_SPEC))
        self.p.write("tests/test_mean.py", "# covers: MEAN-1\n")
        self.p.write("e2e/mean_test.sh", "# covers: MEAN-2\n")
        out = self.p.hook("pre-tool", as_agent(None, "Bash", command="git add -A && git commit -m feat"))
        self.assertIsNone(decision(out), out)
        state = regista.load_json(regista.requirements_path(self.p.root), {})
        for rid in ("MEAN-1", "MEAN-2"):  # precondition: every requirement of the written spec passes
            self.assertTrue(state.get(rid, {}).get("passes"), (rid, state))

    def tearDown(self):
        if getattr(self, "p", None):
            self.p.cleanup()

    def run_line(self, name):
        out = self.p.cli("run").stdout
        lines = [ln for ln in out.splitlines() if name in ln]
        self.assertTrue(lines, out)
        return lines[0], out

    def test_unticked_steps_keep_the_milestone_open(self):  # covers: FIX-10
        self.make(PLAN_OPEN)
        r = self.p.cli("milestone", "M0")
        text = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, text)
        self.assertNotIn("DONE", text)
        self.assertRegex(text, re.compile(r"\b2\b[^\n]*\bsteps?\b", re.IGNORECASE),
                         "the output should name the 2 unticked steps")

    def test_run_lists_a_milestone_with_unticked_steps_as_open(self):  # covers: FIX-10
        self.make(PLAN_OPEN)
        line, out = self.run_line("M0 — Basics")
        self.assertIn("gate open", line, out)
        self.assertNotIn("DONE", line, out)
        self.assertNotIn("waits only on the user's checks", line, out)

    def test_stop_hook_lists_a_milestone_with_unticked_steps_as_open(self):  # covers: FIX-10
        self.make(PLAN_OPEN)
        r = self.p.cli("run", "start")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = self.p.hook("stop", {}) or {}
        self.assertEqual(out.get("decision"), "block", out)
        self.assertIn("build-all run is active", out["reason"])
        lines = [ln for ln in out["reason"].splitlines() if "M0 — Basics" in ln]
        self.assertTrue(lines, out["reason"])
        self.assertIn("gate open", lines[0], out["reason"])
        self.assertNotIn("DONE", lines[0], out["reason"])

    def test_all_steps_ticked_and_requirements_passing_is_done(self):  # covers: FIX-10
        self.make(PLAN_TICKED)
        r = self.p.cli("milestone", "M0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        line, out = self.run_line("M0 — Basics")
        self.assertIn("DONE (gate met)", line, out)


if __name__ == "__main__":
    unittest.main()
