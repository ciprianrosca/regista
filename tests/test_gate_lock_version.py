"""The gate lock refused commands that only ask a gate's tool for its version or help. On 28 October 2026, with the
gate `python3 -m pytest -q`, the command `python3 -m pytest --version` was refused as a direct gate run. Asking a
tool what it is runs no tests and can't collide with another run; running the gate's tests still has to go through
the harness."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_regista import Project, bash, decision  # noqa: E402

REFUSAL = "gate's command directly"

INFO_FLAGS = ("--version", "-V", "--help", "-h", "--collect-only", "--co")


class GateLockVersion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = Project(gates=[{"name": "tests", "run": "python3 -m pytest -q"}])
        cls.plain = Project(gates=[{"name": "tests", "run": "pytest -q"}])
        cls.npm = Project(gates=[{"name": "tests", "run": "npm test"}])
        cls.ctest = Project(gates=[{"name": "tests", "run": "ctest"}])
        cls.mvn = Project(gates=[{"name": "tests", "run": "mvn test"}])

    @classmethod
    def tearDownClass(cls):
        cls.module.cleanup()
        cls.plain.cleanup()
        cls.npm.cleanup()
        cls.ctest.cleanup()
        cls.mvn.cleanup()

    def check(self, project, cmd):
        out = project.hook("pre-tool", bash(cmd))
        return decision(out), (out or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason", "")

    def assert_allowed(self, project, cmd):
        verdict, reason = self.check(project, cmd)
        self.assertFalse(verdict == "deny" and REFUSAL in reason, f"{cmd!r} was refused: {reason}")

    def assert_refused(self, project, cmd):
        verdict, reason = self.check(project, cmd)
        self.assertEqual(verdict, "deny", f"{cmd!r} was not refused")
        self.assertIn(REFUSAL, reason)
        self.assertTrue(reason.startswith("Regista: this runs the 'tests' gate's command directly"), reason)

    def test_module_pytest_version_and_help_are_allowed(self):  # covers: FIX-7
        for flag in INFO_FLAGS:
            with self.subTest(flag=flag):
                self.assert_allowed(self.module, f"python3 -m pytest {flag}")

    def test_plain_pytest_version_and_help_are_allowed(self):  # covers: FIX-7
        for flag in INFO_FLAGS:
            with self.subTest(flag=flag):
                self.assert_allowed(self.plain, f"pytest {flag}")

    def test_gate_command_itself_is_still_refused(self):  # covers: FIX-7
        self.assert_refused(self.module, "python3 -m pytest -q")
        self.assert_refused(self.plain, "pytest -q")

    def test_test_selecting_variant_is_still_refused(self):  # covers: FIX-7
        self.assert_refused(self.module, "python3 -m pytest -q tests/test_x.py")
        self.assert_refused(self.plain, "pytest -q tests/test_x.py")

    def test_version_chained_with_a_gate_run_is_still_refused(self):  # covers: FIX-7
        for cmd in ("python3 -m pytest -q && python3 -m pytest --version",
                    "python3 -m pytest --version && python3 -m pytest -q"):
            with self.subTest(cmd=cmd):
                self.assert_refused(self.module, cmd)
        self.assert_refused(self.plain, "pytest -q && pytest --version")

    def test_pytest_info_flag_followed_by_a_path_is_still_refused(self):  # covers: FIX-7
        for cmd in ("python3 -m pytest --co tests/test_x.py", "python3 -m pytest --version tests/"):
            with self.subTest(cmd=cmd):
                self.assert_refused(self.module, cmd)

    def test_npm_gate_with_info_flags_is_still_refused(self):  # covers: FIX-7
        for cmd in ("npm test --co", "npm test -V", "npm test --version"):
            with self.subTest(cmd=cmd):
                self.assert_refused(self.npm, cmd)

    def test_ctest_gate_with_verbose_flag_is_still_refused(self):  # covers: FIX-7
        self.assert_refused(self.ctest, "ctest -V")

    def test_mvn_gate_with_version_flag_is_still_refused(self):  # covers: FIX-7
        self.assert_refused(self.mvn, "mvn -V test")


if __name__ == "__main__":
    unittest.main()
