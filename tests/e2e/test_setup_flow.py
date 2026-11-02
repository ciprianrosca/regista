"""End-to-end: set up a sample Python project with the real Regista CLI and send commits through the real
PreToolUse hook. Run: python3 -m unittest discover -s tests/e2e"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import CALC, SampleProject, decision, reason  # noqa: E402


class SetupFlow(unittest.TestCase):
    def setUp(self):
        self.p = SampleProject()
        self.addCleanup(self.p.cleanup)

    def test_init_gates_pass_good_commit_allowed_failing_commit_denied(self):  # covers: SN-1
        p = self.p
        init = p.set_up_regista()
        out = json.loads(init.stdout)
        self.assertIn("gates", out.get("detected", {}), init.stdout)
        self.assertTrue(os.path.isdir(p.path(".regista")))
        self.assertEqual([g["name"] for g in p.config()["gates"]], ["tests"])
        self.assertEqual(p.git("status", "--porcelain").stdout.strip(), "", "setup left uncommitted files")

        # Every gate passes on the fresh project.
        g = p.cli("gate", "--all")
        self.assertEqual(g.returncode, 0, f"gate --all failed:\n{g.stdout}\n{g.stderr}")

        # A code change that keeps the tests passing: the hook lets the commit through.
        p.write("calc.py", CALC + "\n\ndef sub(a, b):\n    return a - b\n")
        cmd = 'git commit -am "add sub"'
        answer = p.hook("pre-tool", p.bash(cmd))
        self.assertNotEqual(decision(answer), "deny", f"good commit denied: {answer}\n{p.last_hook.stderr}")
        p.git("commit", "-q", "-am", "add sub")

        # A change that breaks a test: the hook denies the commit and names the failing gate.
        p.write("calc.py", "def add(a, b):\n    return a - b\n")
        answer = p.hook("pre-tool", p.bash('git commit -am "break add"'))
        self.assertEqual(decision(answer), "deny", f"failing commit not denied: {answer}\n{p.last_hook.stderr}")
        self.assertRegex(reason(answer), r"\btests\b")

    def test_subprocesses_never_see_the_real_home_or_project(self):
        # The harness's own guarantee (not a requirement): even when the tests are started from inside a Regista
        # session, no subprocess can reach this machine's home folder or the real project.
        leak = {"CLAUDE_PROJECT_DIR": "/real/project", "REGISTA_WORKER": "w1", "DAEDALUS_SLOT": "3"}
        with mock.patch.dict(os.environ, leak):
            p = SampleProject()
        self.addCleanup(p.cleanup)
        self.assertNotEqual(p.env["HOME"], os.path.expanduser("~"))
        self.assertTrue(p.env["HOME"].startswith(p.base))
        self.assertEqual([k for k in p.env if k == "CLAUDE_PROJECT_DIR" or k.startswith(("REGISTA_", "DAEDALUS_"))],
                         [])


if __name__ == "__main__":
    unittest.main()
