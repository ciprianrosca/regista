"""Commits run the current milestone's end-to-end tests; the whole suite runs where it matters; a time budget
(lesson 14 of the first parallel builds, 28 October 2026).

FPL's full test run went from 3.5 to 14 minutes (11.5 of them browser tests), and every commit waited for it: a
one-line change ran the whole browser suite.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_gate_scope -v"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# test_regista points HOME and CODEX_HOME at a temporary folder when it is imported.
from test_regista import Project, as_agent, decision, regista  # noqa: E402

PLAN = "# Plan\n\n## M1 — Lists\n- [ ] list page\n- [ ] list filters\n\n## M2 — Reports\n- [ ] report page\n"
SPECS = {"lists": {"milestone": "M1", "title": "Lists", "requirements": [
             {"id": "LIST-1", "text": "WHEN opened the list page SHALL show the items", "verify": "e2e"},
             {"id": "LIST-2", "text": "WHEN empty the list SHALL say so", "verify": "test"}]},
         "reports": {"milestone": "M2", "title": "Reports", "requirements": [
             {"id": "REP-1", "text": "WHEN opened the report page SHALL show totals", "verify": "e2e"}]}}


class Scope(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="regista-scope-")
        self.log = os.path.join(self.out, "log")

    def tearDown(self):
        self.p.cleanup()
        shutil.rmtree(self.out, ignore_errors=True)

    def project(self, plan=PLAN, e2e_exit=0, **cfg):
        e2e = {"name": "e2e", "run": f"echo all >> '{self.log}'; exit {e2e_exit}",
               "run_some": f"echo some {{files}} >> '{self.log}'; exit {e2e_exit}",
               "evidence": "e2e", "tests": ["e2e/**"]}
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}, e2e], **cfg)
        self.p.write(".regista/plan.md", plan)
        for name, spec in SPECS.items():
            self.p.write(f".regista/specs/{name}.json", json.dumps(spec))
        self.p.write("tests/test_lists.py", "# covers: LIST-2\n")
        self.p.write("e2e/lists.spec.js", "// covers: LIST-1\n")
        self.p.write("e2e/reports.spec.js", "// covers: REP-1\n")
        self.p.write("e2e/totals.spec.js", "// covers: REP-1\n")
        self.p.commit("src/app.py", "x = 1\n", "start")

    def commit(self):
        return self.p.hook("pre-tool", as_agent(None, "Bash", command="git add -A && git commit -m step"))

    def ran(self):
        with open(self.log, encoding="utf-8") as f:
            return f.read().split("\n")[:-1]

    def state(self):
        return regista.load_json(regista.requirements_path(self.p.root), {})

    def test_a_step_commit_runs_only_its_milestones_browser_tests(self):
        self.project()
        self.p.write("src/app.py", "x = 2\n")
        self.p.write(".regista/specs/lists.json", json.dumps({**SPECS["lists"], "title": "Lists, sorted"}))
        out = self.commit()
        self.assertNotEqual(decision(out), "deny", out)
        self.assertEqual(self.ran(), ["some e2e/lists.spec.js"])
        state = self.state()
        self.assertTrue(state["LIST-1"]["passes"])
        self.assertNotIn("at", state["REP-1"])  # its tests didn't run: no result is recorded for it
        # the partial pass doesn't stand in for the whole suite: `regista.py gate` runs it
        self.p.cli("gate")
        self.assertEqual(self.ran()[-1], "all")

    def test_a_changed_browser_test_runs_too(self):
        self.project()
        self.p.write(".regista/specs/lists.json", json.dumps({**SPECS["lists"], "title": "Lists, sorted"}))
        self.p.write("e2e/helpers.js", "export const login = () => {}\n")  # covers nothing, but changed
        self.assertNotEqual(decision(self.commit()), "deny")
        self.assertEqual(self.ran(), ["some e2e/helpers.js e2e/lists.spec.js"])

    def test_when_the_milestone_is_unknown_or_closing_the_whole_suite_runs(self):
        self.project()
        self.p.write("src/app.py", "x = 2\n")  # no spec, no test, no plan `Files:` line names it
        self.assertNotEqual(decision(self.commit()), "deny")
        self.assertEqual(self.ran(), ["all"])
        self.p.cleanup()
        self.project(plan=PLAN.replace("- [ ] list filters\n", "- [x] list filters\n"))  # M1's last step
        self.p.write("src/app.py", "x = 2\n")
        self.p.write(".regista/specs/lists.json", json.dumps({**SPECS["lists"], "title": "Lists, sorted"}))
        self.assertNotEqual(decision(self.commit()), "deny")
        self.assertEqual(self.ran(), ["all", "all"])

    def test_plan_files_and_a_gate_without_run_some(self):
        self.project(plan=PLAN.replace("## M2 — Reports\n", "## M2 — Reports\nFiles: src/reports/**\n- [ ] totals\n"))
        cfg = regista.load_config(self.p.root)
        self.assertEqual(regista.work_milestones(self.p.root, cfg, ["src/reports/total.py"]), ["M2"])
        gates = regista.select_gates(cfg, event="commit")
        scoped = regista.scope_e2e_gates(self.p.root, cfg, gates, ["src/reports/total.py"])
        self.assertEqual(scoped[1]["_only"], ["e2e/reports.spec.js", "e2e/totals.spec.js"])
        plain = [gates[0], {k: v for k, v in gates[1].items() if k != "run_some"}]
        self.assertEqual(regista.scope_e2e_gates(self.p.root, cfg, plain, ["src/reports/total.py"]), plain)

    def test_a_partial_failure_records_only_what_ran(self):
        self.project()
        cfg = regista.load_config(self.p.root)
        regista.record_requirements(self.p.root, cfg, [{"name": "tests", "status": "pass"},
                                                       {"name": "e2e", "status": "pass"}])
        self.assertTrue(self.state()["REP-1"]["passes"])
        regista.record_requirements(self.p.root, cfg, [
            {"name": "tests", "status": "pass"},
            {"name": "e2e", "status": "fail", "only": ["e2e/lists.spec.js"], "of": 2, "milestones": ["M1"]}])
        self.assertFalse(self.state()["LIST-1"]["passes"])
        self.assertTrue(self.state()["REP-1"]["passes"])

    def test_a_deploy_needs_the_whole_browser_suite(self):
        self.project(e2e_exit=1, deploy={"run": f"echo deployed >> '{self.log}'"})
        r = self.p.cli("deploy")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("whole end-to-end suite must pass before a deploy", r.stdout + r.stderr)
        self.assertEqual(self.ran(), ["all"])


class SomeCommand(unittest.TestCase):
    def tearDown(self):
        self.p.cleanup()

    def test_running_the_partial_command_goes_through_the_harness_too(self):
        self.p = Project(gates=[{"name": "e2e", "run": "npm run e2e --silent", "run_some": "bash e2e/some.sh {files}",
                                 "evidence": "e2e", "tests": ["e2e/**"]}])
        out = self.p.hook("pre-tool", as_agent("builder", "Bash", command="bash e2e/some.sh e2e/a.spec.js"))
        self.assertEqual(decision(out), "deny")
        self.assertIn("gate e2e", out["hookSpecificOutput"]["permissionDecisionReason"])


class Budget(unittest.TestCase):
    def tearDown(self):
        self.p.cleanup()

    def test_a_slow_run_warns_and_names_the_cleanup(self):
        self.p = Project(gates=[{"name": "tests", "run": "sleep 1.2"}], gates_budget_seconds=1)
        self.p.write("src/app.py", "x = 2\n")
        out = self.p.hook("pre-tool", as_agent(None, "Bash", command="git add -A && git commit -m step"))
        self.assertNotEqual(decision(out), "deny")  # it only warns
        note = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("over the project's budget of 1 s", note)
        self.assertIn("`regista.py tests cleanup`", note)
        self.assertIn("tests cleanup", regista.resume_text(self.p.root, regista.load_config(self.p.root)))
        self.assertIn("tests cleanup", self.p.cli("gate", "--fresh").stdout)

    def test_per_gate_budget_and_no_warning_within_it(self):
        self.p = Project()
        cfg = {"gates_budget_seconds": 900}
        gates = [{"name": "unit", "budget_seconds": 60}, {"name": "e2e", "budget_seconds": 300}]
        results = [{"name": "unit", "status": "pass", "seconds": 30}, {"name": "e2e", "status": "pass", "seconds": 690}]
        note = regista.budget_note(cfg, gates, results)
        self.assertIn("e2e took 11.5 minutes, over its budget of 5 minutes", note)
        self.assertNotIn("unit took", note)
        self.assertNotIn("in all", note)
        results[1]["reused"] = "2026-10-28T10:00:00"  # a reused result took no time
        self.assertEqual(regista.budget_note(cfg, gates, results), "")


if __name__ == "__main__":
    unittest.main()
