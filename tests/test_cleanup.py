"""`regista.py tests cleanup`: overlapping tests are listed, proven to add nothing by breaking the code they guard on
a scratch copy, and removed only with the user's typed OK. Agents can't run it, and the building flow can't drop
tests without the user. Run: python -m unittest tests.test_cleanup -q"""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# test_regista points HOME and CODEX_HOME at a temporary folder when it is imported.
from test_regista import PY, TOOL, Project, as_agent, decision, regista, sh  # noqa: E402

CALC = """def clamp(x, low, high):
    if x < low:
        return low
    if x > high:
        return high
    return x
"""

CALC_TESTS = """import sys
import unittest
sys.path.insert(0, "src")
from calc import clamp


class Clamp(unittest.TestCase):
    def test_below(self):
        self.assertEqual(clamp(-5, 0, 10), 0)
        self.assertEqual(clamp(5, 0, 10), 5)

    def test_below_again(self):
        self.assertEqual(clamp(-5, 0, 10), 0)
        self.assertEqual(clamp(5, 0, 10), 5)

    def test_above(self):
        self.assertEqual(clamp(15, 0, 10), 10)
"""

GRADE = """def grade(score):
    if score >= 90:
        return "A"
    if score >= 50:
        return "pass"
    return "fail"
"""

# Same requirement, same code, but test_top is the only test of the top grade: it must not be proven redundant.
GRADE_TESTS = """import sys
import unittest
sys.path.insert(0, "src")
from grade import grade


class Grade(unittest.TestCase):
    # covers: GRADE-1
    def test_top(self):
        self.assertEqual(grade(95), "A")

    # covers: GRADE-1
    def test_fail(self):
        self.assertEqual(grade(10), "fail")
        self.assertEqual(grade(60), "pass")
"""

PAGES = """const pages = ['/', '/about', '/contact'];
for (const page of pages) {
  test(`the logo shows on ${page}`, async () => {
    expect(page).toBeTruthy();
  });
}
"""


class TestsCleanup(unittest.TestCase):
    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": f'"{PY}" -m unittest discover -s tests -q', "when": ["commit"]},
                                {"name": "e2e", "run": "exit 1", "when": ["milestone"], "evidence": "e2e",
                                 "tests": ["e2e/**"]}])
        self.p.write("src/calc.py", CALC)
        self.p.write("src/grade.py", GRADE)
        self.p.write("tests/test_calc.py", CALC_TESTS)
        self.p.write("tests/test_grade.py", GRADE_TESTS)
        self.p.write("e2e/pages.test.js", PAGES)
        sh(self.p.root, "git", "add", "-A")
        sh(self.p.root, "git", "commit", "-q", "-m", "app")

    def tearDown(self):
        self.p.cleanup()

    def read(self, rel):
        with open(os.path.join(self.p.root, rel), encoding="utf-8") as f:
            return f.read()

    def propose(self):
        r = self.p.cli("tests", "cleanup")
        self.assertEqual(r.returncode, 0, r.stderr)
        folder = os.path.join(self.p.root, ".regista", "cleanup")
        name = next(f for f in sorted(os.listdir(folder)) if f.endswith(".json"))
        return name, regista.load_json(os.path.join(folder, name), {})

    def test_propose_lists_the_overlaps_and_touches_nothing(self):
        name, prop = self.propose()
        names = {c["name"]: c for c in prop["candidates"]}
        self.assertEqual(set(names), {"Clamp::test_below_again", "Grade::test_top"})
        self.assertEqual(names["Clamp::test_below_again"]["keeper"], "tests/test_calc.py::Clamp::test_below")
        self.assertIn("same requirements (GRADE-1)", names["Grade::test_top"]["why"])
        self.assertEqual(names["Grade::test_top"]["guards"], ["src/grade.py"])
        self.assertTrue(any("once for every item" in s["why"] for s in prop["suggestions"]), prop["suggestions"])
        md = self.read(".regista/cleanup/" + name.replace(".json", ".md"))
        self.assertIn("Why it could go", md)
        self.assertIn("What replaces it", md)
        self.assertIn("Nothing has been removed", md)
        # only the proposal is new: no test or source file changed
        self.assertEqual(set(sh(self.p.root, "git", "status", "--porcelain", "--untracked-files=all").split()),
                         {"??", ".regista/cleanup/" + name.replace(".json", ".md"), ".regista/cleanup/" + name})

    def test_prove_breaks_the_code_on_a_copy_and_keeps_tests_that_catch_something(self):
        name, _ = self.propose()
        r = self.p.cli("tests", "cleanup", "--prove", name)
        self.assertEqual(r.returncode, 0, r.stderr)
        prop = regista.load_json(os.path.join(self.p.root, ".regista", "cleanup", name), {})
        c = {x["name"]: x for x in prop["candidates"]}
        self.assertEqual(c["Clamp::test_below_again"]["status"], "proven", c["Clamp::test_below_again"])
        self.assertTrue(c["Clamp::test_below_again"]["mutations"])
        self.assertEqual(c["Grade::test_top"]["status"], "not proven")
        self.assertIn("no longer notice", c["Grade::test_top"]["proof"])
        self.assertIn("1 of 2 candidates are proven", r.stdout)
        # the project itself was never changed: the breaks happened on a scratch copy
        self.assertEqual(self.read("src/calc.py"), CALC)
        self.assertEqual(self.read("src/grade.py"), GRADE)
        self.assertEqual(sh(self.p.root, "git", "status", "--porcelain", "--", "src", "tests"), "")

    def test_prove_respects_its_time_budget(self):
        name, _ = self.propose()
        path, prop = regista.cleanup_load(self.p.root, name)
        cfg = regista.load_config(self.p.root)
        cfg["gates"][0]["run"] = f'"{PY}" -c "import time; time.sleep(0.4)"'
        with self.assertRaises(SystemExit) as e:  # the unchanged run alone takes longer than the budget
            regista.cleanup_prove(self.p.root, cfg, path, prop, budget=0, say=lambda *_: None)
        self.assertIn("within the time budget", str(e.exception))

    def test_apply_removes_only_proven_tests_after_a_typed_yes_and_commits_the_proof(self):
        name, _ = self.propose()
        self.p.cli("tests", "cleanup", "--prove", name)
        cfg = regista.load_config(self.p.root)
        path, prop = regista.cleanup_load(self.p.root, name)
        top = next(c["id"] for c in prop["candidates"] if c["name"] == "Grade::test_top")
        ok, lines = regista.cleanup_apply(self.p.root, cfg, path, prop, [top], confirm=lambda _: "yes")
        self.assertFalse(ok)
        self.assertIn("isn't proven", lines[0])
        # without a person at a terminal it refuses
        r = self.p.cli("tests", "cleanup", "--apply", name)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("typed in a terminal", r.stdout)
        ok, lines = regista.cleanup_apply(self.p.root, cfg, path, prop, confirm=lambda _: "no")
        self.assertFalse(ok)
        self.assertEqual(self.read("tests/test_calc.py"), CALC_TESTS)
        ok, lines = regista.cleanup_apply(self.p.root, cfg, path, prop, confirm=lambda _: "yes")
        self.assertTrue(ok, lines)
        text = self.read("tests/test_calc.py")
        self.assertNotIn("test_below_again", text)
        self.assertIn("def test_below(", text)
        self.assertIn("def test_above(", text)
        self.assertIn("def test_top(", self.read("tests/test_grade.py"))
        msg = sh(self.p.root, "git", "log", "-1", "--format=%B")
        self.assertIn("Remove 1 overlapping test, proven to add nothing", msg)
        self.assertIn("test_below_again", msg)
        self.assertIn("still caught every one", msg)
        self.assertEqual(sh(self.p.root, "git", "status", "--porcelain", "--", "tests"), "")
        # the remaining tests still pass
        r = subprocess.run([PY, "-m", "unittest", "discover", "-s", "tests", "-q"], cwd=self.p.root,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_apply_refuses_when_files_changed_after_the_proof(self):
        name, _ = self.propose()
        self.p.cli("tests", "cleanup", "--prove", name)
        self.p.commit("src/calc.py", CALC.replace("return x\n", "return x  # unchanged behaviour\n"))
        path, prop = regista.cleanup_load(self.p.root, name)
        ok, lines = regista.cleanup_apply(self.p.root, regista.load_config(self.p.root), path, prop,
                                          confirm=lambda _: "yes")
        self.assertFalse(ok)
        self.assertIn("changed after the proof", lines[0])
        self.assertEqual(self.read("tests/test_calc.py"), CALC_TESTS)

    def test_agents_cant_run_it_or_write_its_proofs(self):
        for role in (None, "builder", "test-writer", "reviewer"):
            for cmd in (f'python "{TOOL}" tests cleanup', f'python3 "{TOOL}" tests cleanup --apply x.json c1'):
                out = self.p.hook("pre-tool", as_agent(role, "Bash", command=cmd))
                self.assertEqual(decision(out), "deny", (role, cmd))
            self.assertIn("the user's call", out["hookSpecificOutput"]["permissionDecisionReason"])
        out = self.p.hook("pre-tool", as_agent(None, "Write", file_path=os.path.join(
            self.p.root, ".regista", "cleanup", "2026-10-28.json"), content="{}"))
        self.assertEqual(decision(out), "deny")


@unittest.skipUnless(shutil.which("node"), "needs Node.js")
class NodeTestsCleanup(unittest.TestCase):
    def test_node_tests_are_proposed_and_proven(self):
        p = Project(gates=[{"name": "tests", "run": "node --test", "when": ["commit"]}])
        try:
            p.write("src/price.mjs", "export function discount(total) {\n  if (total >= 100) {\n    return total - 10;\n"
                                     "  }\n  return total;\n}\n")
            body = "  assert.equal(discount(150), 140);\n  assert.equal(discount(50), 50);\n"
            p.write("tests/price.test.mjs", "import { test } from 'node:test';\nimport assert from 'node:assert/strict';\n"
                                            "import { discount } from '../src/price.mjs';\n\n"
                                            f"test('ten off big orders', () => {{\n{body}}});\n\n"
                                            f"test('ten off big orders, again', () => {{\n{body}}});\n")
            sh(p.root, "git", "add", "-A")
            sh(p.root, "git", "commit", "-q", "-m", "app")
            self.assertEqual(p.cli("tests", "cleanup").returncode, 0)
            path, prop = regista.cleanup_load(p.root, os.path.join(p.root, ".regista", "cleanup",
                                                                    regista.time.strftime("%Y-%m-%d") + ".json"))
            self.assertEqual([(c["name"], c["guards"]) for c in prop["candidates"]],
                             [("ten off big orders, again", ["src/price.mjs"])])
            r = p.cli("tests", "cleanup", "--prove", os.path.basename(path))
            self.assertIn("1 of 1 candidates are proven", r.stdout, r.stderr)
        finally:
            p.cleanup()


class CommitsThatDropTests(unittest.TestCase):
    def setUp(self):
        self.p = Project(gates=[])
        self.p.commit("tests/test_a.py", "def test_one():\n    assert 1\n\n\ndef test_two():\n    assert 2\n")

    def tearDown(self):
        self.p.cleanup()

    def commit(self):
        return self.p.hook("pre-tool", as_agent(None, "Bash", command="git add -A && git commit -m x"))

    def test_removing_a_test_needs_the_user(self):
        self.p.write("tests/test_a.py", "def test_one():\n    assert 1\n")
        out = self.commit()
        self.assertEqual(decision(out), "ask")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("removes 1 test (tests/test_a.py, test_two)", reason)
        self.assertIn("tests cleanup", reason)

    def test_deleting_a_test_file_needs_the_user(self):
        os.remove(os.path.join(self.p.root, "tests", "test_a.py"))
        self.assertEqual(decision(self.commit()), "ask")

    def test_adding_renaming_or_moving_tests_is_fine(self):
        self.p.write("tests/test_a.py", "def test_one():\n    assert 1\n\n\ndef test_two_renamed():\n    assert 2\n")
        self.p.write("tests/test_b.py", "def test_three():\n    assert 3\n")
        self.assertIsNone(decision(self.commit()))
        sh(self.p.root, "git", "add", "-A")
        sh(self.p.root, "git", "commit", "-q", "-m", "more")
        sh(self.p.root, "git", "mv", "tests/test_b.py", "tests/test_c.py")
        self.assertIsNone(decision(self.commit()))


class FindingTests(unittest.TestCase):
    def test_js_tests_end_at_their_closing_bracket(self):
        text = "describe('x', () => {\n  // covers: A-1\n  it('one', () => {\n    f(')');\n  });\n  it('two', () => {});\n});\n"
        tests = regista.find_tests(text, "js")
        self.assertEqual([(t["name"], t["start"], t["end"]) for t in tests], [("one", 1, 5), ("two", 5, 6)])
        self.assertEqual(regista._strip_tests(text, [(1, 5)]),  # the describe still closes, once
                         "describe('x', () => {\n  it('two', () => {});\n});\n")

    def test_mutations_are_deterministic_and_never_inside_strings(self):
        src = 'def f(x):\n    if x > 3:\n        return "a < b"\n    return x == 2\n'
        muts = regista.mutations_for(src, "py", 8)
        self.assertEqual(muts, regista.mutations_for(src, "py", 8))
        self.assertEqual([m[1].strip() for m in muts], ["if x >= 3:", "return None", "return x != 2"])


if __name__ == "__main__":
    unittest.main()
