"""Tests for the init --auto detection follow-ups (FIX-11): pytest settings files keep the pytest gate, and the
unittest gate runs discover on every folder that holds test files when they sit outside tests/, test/ or the root.
And for the milestone step follow-up (FIX-12): the template placeholder step `<step>` doesn't hold a milestone open,
and the reason for unticked steps says how to close them.
Run: python -m unittest tests.test_init_detection_2 -v"""
import json
import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_init_detection import PYTHONS, DetectCase  # noqa: E402  (also isolates HOME for every tool run)
import test_milestone_steps as milestone_steps  # noqa: E402  (module import, so its tests don't run twice here)
from test_regista import PY, TOOL, regista, sh  # noqa: E402

TESTS_OF = lambda n: "import unittest\n\nclass T(unittest.TestCase):\n" + "".join(  # noqa: E731
    "    def test_%d(self):\n        self.assertTrue(True)\n" % i for i in range(n))


class PytestSettings(DetectCase):
    def assertPytestGate(self, run):
        self.assertIn("pytest -q", run)
        self.assertNotIn("unittest", run)

    def project(self, name, text):
        root = self.fresh()
        self.write(name, text, root=root)
        self.write("calc.py", "", root=root)
        self.write("tests/test_calc.py", TESTS_OF(1), root=root)
        return root

    def test_each_pytest_settings_file_keeps_pytest(self):  # covers: FIX-11
        for name, text in (("pytest.ini", "[pytest]\naddopts = -q\n"),
                           ("tox.ini", "[tox]\nenvlist = py312\n\n[pytest]\ntestpaths = tests\n"),
                           ("setup.cfg", "[metadata]\nname = calc\n\n[tool:pytest]\ntestpaths = tests\n"),
                           ("pyproject.toml", '[project]\nname="calc"\ndependencies=[]\n\n'
                                              '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')):
            with self.subTest(file=name):
                self.assertPytestGate(self.gate_run(self.project(name, text)))

    def test_settings_files_without_a_pytest_section_do_not(self):  # covers: FIX-11
        for name, text in (("tox.ini", "[tox]\nenvlist = py312\n\n[testenv]\ncommands = python -m unittest\n"),
                           ("setup.cfg", "[metadata]\nname = calc\n\n[flake8]\nmax-line-length = 120\n"),
                           ("pyproject.toml", '[project]\nname="calc"\ndependencies=[]\n\n'
                                              '[tool.ruff]\nline-length = 120\n')):
            with self.subTest(file=name):
                run = self.gate_run(self.project(name, text))
                self.assertIn("-m unittest discover", run)
                self.assertNotIn("pytest", run)


class UnittestGateFolders(DetectCase):
    def folders(self, run):
        """The folders a '<python> -m unittest discover -s <dir>' chain (joined with &&) runs, in order."""
        found = []
        for part in run.split(" && "):
            m = re.fullmatch(r"(.+?) -m unittest discover -s (\S+)", part.strip())
            self.assertIsNotNone(m, "expected '<python> -m unittest discover -s <dir>' parts, got %r" % run)
            self.assertIn(m.group(1), PYTHONS, run)
            found.append(os.path.normpath(m.group(2)).replace(os.sep, "/"))
        return found

    def run_gate(self, run):
        """Run the proposed gate in the sample project; return the total of the 'Ran N tests' lines."""
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        p = subprocess.run(run, shell=True, cwd=self.root, env=env, capture_output=True, text=True, timeout=120)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, "the gate %r failed:\n%s" % (run, out))
        self.assertNotIn("NO TESTS RAN", out)
        return sum(int(n) for n in re.findall(r"^Ran (\d+) tests?", out, re.M))

    def test_test_files_in_scripts_only(self):  # covers: FIX-11
        self.write("scripts/tool.py", "def add(a, b):\n    return a + b\n")
        self.write("scripts/test_tool.py", TESTS_OF(1))
        self.write("scripts/test_more.py", TESTS_OF(2))
        run = self.gate_run()
        self.assertEqual(self.folders(run), ["scripts"])
        self.assertEqual(self.run_gate(run), 3)

    def test_test_files_in_two_folders(self):  # covers: FIX-11
        self.write("calc/core.py", "def add(a, b):\n    return a + b\n")
        self.write("calc/tests/test_core.py", TESTS_OF(2))
        self.write("tools/helper.py", "")
        self.write("tools/test_helper.py", TESTS_OF(1))
        self.assertFalse(os.path.exists(os.path.join(self.root, "calc", "tests", "__init__.py")))
        run = self.gate_run()
        self.assertEqual(self.folders(run), ["calc/tests", "tools"])
        self.assertEqual(self.run_gate(run), 3)

    def discovers(self, run):
        """The (folder, top-level) pairs a '<python> -m unittest discover -s <dir> [-t .]' chain runs, in order;
        the folder is kept as written (so './-x' stays './-x'), the top level is None when there's no -t."""
        found = []
        for part in run.split(" && "):
            m = re.fullmatch(r"(.+?) -m unittest discover -s (\S+)(?: -t (\S+))?", part.strip())
            self.assertIsNotNone(m, "expected '<python> -m unittest discover -s <dir> [-t .]' parts, got %r" % run)
            self.assertIn(m.group(1), PYTHONS, run)
            found.append((m.group(2), m.group(3)))
        return found

    def test_package_test_folder_runs_with_the_project_root_as_top_level(self):  # covers: FIX-11
        self.write("calc/__init__.py", "")
        self.write("calc/core.py", "def add(a, b):\n    return a + b\n")
        self.write("calc/tests/__init__.py", "")
        self.write("calc/tests/test_core.py",
                   "import unittest\nfrom ..core import add\n\n\nclass T(unittest.TestCase):\n"
                   "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
                   "    def test_add_negative(self):\n        self.assertEqual(add(-2, 1), -1)\n")
        self.write("tools/test_t.py", TESTS_OF(1))
        run = self.gate_run()
        self.assertEqual(self.discovers(run), [("calc/tests", "."), ("tools", None)])
        self.assertEqual(self.run_gate(run), 3, "every test should run exactly once")

    def test_folder_named_with_a_leading_dash_is_written_as_a_path(self):  # covers: FIX-11
        self.write("-x/test_x.py", TESTS_OF(2))
        run = self.gate_run()
        self.assertEqual(self.discovers(run), [("./-x", None)])
        self.assertEqual(self.run_gate(run), 2)

    def test_nested_plain_test_folders_each_run_once(self):  # covers: FIX-11
        self.write("a/tests/test_outer.py", TESTS_OF(1))
        self.write("a/tests/sub/test_inner.py", TESTS_OF(2))
        run = self.gate_run()
        self.assertEqual(self.discovers(run), [("a/tests", None), ("a/tests/sub", None)])
        self.assertEqual(self.run_gate(run), 3, "each test should run exactly once: %r" % run)


# Every real M0 step ticked, plus the template's placeholder line left in.
PLAN_PLACEHOLDER = milestone_steps.PLAN_TICKED.replace("- [x] spread\n", "- [x] spread\n- [ ] <step>\n")
# Every real M0 step ticked, plus a real step whose whole text happens to be in angle brackets.
PLAN_ANGLE_STEP = milestone_steps.PLAN_TICKED.replace("- [x] spread\n", "- [x] spread\n- [ ] <Login page>\n")


class PlaceholderSteps(unittest.TestCase):
    """A throwaway project whose M0 requirements all pass, set up as tests/test_milestone_steps.py does."""
    make = milestone_steps.MilestoneOpenSteps.make
    tearDown = milestone_steps.MilestoneOpenSteps.tearDown
    run_line = milestone_steps.MilestoneOpenSteps.run_line

    def milestone(self):
        r = self.p.cli("milestone", "M0")
        return r.returncode, r.stdout + r.stderr

    def test_placeholder_step_does_not_keep_the_milestone_open(self):  # covers: FIX-12
        self.assertIn("- [ ] <step>\n", PLAN_PLACEHOLDER)
        self.make(PLAN_PLACEHOLDER)
        code, text = self.milestone()
        self.assertEqual(code, 0, text)
        line, out = self.run_line("M0 — Basics")
        self.assertIn("DONE (gate met)", line, out)

    def test_step_in_angle_brackets_still_keeps_it_open(self):  # covers: FIX-12
        self.assertIn("- [ ] <Login page>\n", PLAN_ANGLE_STEP)
        self.make(PLAN_ANGLE_STEP)
        code, text = self.milestone()
        self.assertNotEqual(code, 0, text)
        self.assertNotIn("DONE", text)
        self.assertRegex(text, re.compile(r"\b1\b[^\n]*\bsteps?\b", re.IGNORECASE),
                         "the output should name the 1 unticked step")

    def test_unticked_reason_says_how_to_close_the_steps(self):  # covers: FIX-12
        self.make(milestone_steps.PLAN_OPEN)
        code, text = self.milestone()
        self.assertNotEqual(code, 0, text)
        self.assertIn("plan tick M0", text, "the reason should say how to tick a built step")
        self.assertRegex(text, re.compile(r"remov\w*[^\n]*\bplan\b", re.IGNORECASE),
                         "the reason should say to remove a dropped step from the plan")

    def worker(self):
        """Commit the project, then make a worker for M0 in its own worktree the way `fleet spawn` lays one out
        (branch regista/m0, .regista/worker.json naming the overseer). Returns the worker's folder."""
        root = self.p.root
        sh(root, "git", "add", "-A")
        sh(root, "git", "commit", "-q", "-m", "feat: mean")
        with open(os.path.join(root, ".git", "info", "exclude"), "a", encoding="utf-8") as f:
            f.write("# Regista workers\n.regista/worktrees/\n")
        wt = os.path.join(root, ".regista", "worktrees", "m0")
        sh(root, "git", "worktree", "add", "-q", wt, "-b", "regista/m0")
        with open(os.path.join(wt, ".regista", "worker.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "m0", "milestone": "M0", "slot": 1, "overseer_root": root, "branch": "regista/m0"}, f)
        return wt

    def test_in_a_worker_the_reason_says_the_overseer_ticks_on_merge(self):  # covers: FIX-12
        self.make(milestone_steps.PLAN_OPEN)
        wt = self.worker()
        env = {k: v for k, v in os.environ.items() if k not in regista.HOST_PROJECT_VARS}
        env.update(REGISTA_WORKER="m0", REGISTA_SLOT="1")
        r = subprocess.run([PY, TOOL, "milestone", "M0"], cwd=wt, env=env,
                           capture_output=True, text=True, encoding="utf-8")
        text = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, text)
        self.assertRegex(text, re.compile(r"\b2\b[^\n]*\bsteps?\b", re.IGNORECASE), "the 2 unticked steps")
        lines = [ln for ln in text.splitlines() if "overseer" in ln.lower()]
        self.assertTrue(lines, "a worker's reason should say the overseer ticks the steps:\n" + text)
        self.assertRegex(lines[0], re.compile(r"tick", re.IGNORECASE), text)
        self.assertRegex(lines[0], re.compile(r"merg", re.IGNORECASE), "it ticks them when it merges the worker")
        self.assertNotIn("plan tick", text, "a worker may not run `plan tick`, so the reason shouldn't send it there")

    def test_run_keeps_the_unticked_reason_apart_from_the_other_reasons(self):  # covers: FIX-12
        self.make(milestone_steps.PLAN_OPEN)
        # a second M0 requirement that no test covers, so the milestone has another open reason too
        self.p.write(".regista/specs/median.json", json.dumps({"step": "median", "milestone": "M0", "title": "Median",
                     "requirements": [{"id": "MED-1", "text": "WHEN the list is empty median SHALL raise ValueError",
                                       "verify": "test"}]}))
        line, out = self.run_line("M0 — Basics")
        self.assertIn("unticked", line, out)
        self.assertRegex(line, r"\b1 other reasons?\b", out)
        # seen on 28 October 2026: '... or remove a dropped step from the plan, 1 other reason(s)', which reads as
        # if the count belonged to the step advice; the count needs its own clause (after ';' or '.', or before it)
        self.assertNotRegex(line, r"plan,\s*\d+ other reason", out)
        self.assertRegex(line, r"[;.]\s*(?:and |plus )?\d+ other reason|\d+ other reasons?\W[^\n]*unticked", out)


if __name__ == "__main__":
    unittest.main()
