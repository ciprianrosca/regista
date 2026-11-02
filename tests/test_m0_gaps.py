"""Two small gaps the M0 reviewers found.

FIX-13: the gate lock (an agent may not run a gate's test command itself) recognises the gate's pytest command when
the agent wraps it in a runner (`uv run`, `uv run --frozen`, `poetry run`), and the bare command when the gate itself
is written with `uv run`. Asking only for the version or help stays allowed, as it is for the bare command.

FIX-14: on Python older than 3.11 there is no tomllib, so init and migrate can't edit ruff's settings safely. They
say so once, plainly, and give the extend-exclude lines to add by hand, instead of one warning per ruff file. The
README's requirements line says this one step needs 3.11 while the rest runs on 3.10.

Python without tomllib is simulated by putting a folder on PYTHONPATH whose tomllib.py raises ModuleNotFoundError,
so `import tomllib` fails in the tool's process the way it does on Python 3.10.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_m0_gaps -v"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# test_regista points HOME and CODEX_HOME at a temporary folder when it is imported.
from test_regista import PY, TOOL, Project, bash, clean_env, decision  # noqa: E402
from test_migration import OldProject, read_bytes, said  # noqa: E402
from test_linters_exclude import ENTRIES, PYPROJECT, RUFF_TOML, fresh_repo  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REFUSAL = "gate's command directly"


# --- FIX-13: the gate lock sees through uv run and poetry run --------------------------------------------------------

class GateLockRunners(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = Project(gates=[{"name": "tests", "run": "python3 -m pytest -q"}])
        cls.uv = Project(gates=[{"name": "tests", "run": "uv run pytest -q"}])

    @classmethod
    def tearDownClass(cls):
        cls.module.cleanup()
        cls.uv.cleanup()

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

    def test_gate_command_behind_a_runner_is_refused(self):  # covers: FIX-13
        for cmd in ("uv run python3 -m pytest -q", "uv run --frozen python3 -m pytest -q",
                    "poetry run python3 -m pytest -q", "uv run python3 -m pytest -q tests/test_x.py"):
            with self.subTest(cmd=cmd):
                self.assert_refused(self.module, cmd)

    def test_runner_option_with_a_value_is_skipped_too(self):  # covers: FIX-13
        self.assert_refused(self.module, "uv run --with pytest-xdist python3 -m pytest -q")

    def test_version_or_help_behind_a_runner_is_allowed(self):  # covers: FIX-13
        for cmd in ("uv run python3 -m pytest --version", "uv run --frozen python3 -m pytest --help",
                    "poetry run python3 -m pytest -V"):
            with self.subTest(cmd=cmd):
                self.assert_allowed(self.module, cmd)

    def test_uv_gate_run_bare_is_refused(self):  # covers: FIX-13
        for cmd in ("pytest -q", "python3 -m pytest -q", "uv run pytest -q", "poetry run pytest -q"):
            with self.subTest(cmd=cmd):
                self.assert_refused(self.uv, cmd)

    def test_uv_gate_version_query_is_allowed(self):  # covers: FIX-13
        for cmd in ("pytest --version", "uv run pytest --version", "python3 -m pytest --help"):
            with self.subTest(cmd=cmd):
                self.assert_allowed(self.uv, cmd)

    def test_other_runner_commands_stay_allowed(self):  # covers: FIX-13
        for project in (self.module, self.uv):
            for cmd in ("uv sync", "uv run ruff check .", "uv run python3 scripts/tool.py", "poetry install",
                        "poetry run python3 -c 'print(1)'", "uv pip list"):
                with self.subTest(gate=project.root, cmd=cmd):
                    self.assert_allowed(project, cmd)


# --- FIX-14: without tomllib, say once what to add to ruff's settings ----------------------------------------------

def no_tomllib_env(shadow, **extra):
    """A clean environment whose Python can't import tomllib, as on Python 3.10."""
    old = os.environ.get("PYTHONPATH")
    return clean_env(PYTHONPATH=shadow + (os.pathsep + old if old else ""), **extra)


def run_without_tomllib(shadow, cwd, *args, **extra):
    return subprocess.run([PY, TOOL, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          env=no_tomllib_env(shadow, **extra), timeout=180)


def strings(value):
    """Every string inside a parsed JSON value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    return []


def told(r):
    """What a command told the user: its output, with `init --auto`'s JSON answer read as the texts it holds."""
    try:
        return ("\n".join(strings(json.loads(r.stdout))) + "\n" + r.stderr).replace(REPO, "<harness>")
    except ValueError:
        return said(r)


def hand_lines(out):
    """The extend-exclude settings the output gives to add by hand, each parsed as TOML."""
    found = []
    for line in out.splitlines():
        m = re.search(r"extend-exclude\s*=\s*\[[^\]]*\]", line)
        if m:
            try:
                found.append(tomllib.loads(m.group(0)))
            except tomllib.TOMLDecodeError:
                pass
    return found


class RuffSettingsWithoutTomllib(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.shadow = tempfile.mkdtemp(prefix="regista-no-tomllib-")
        with open(os.path.join(cls.shadow, "tomllib.py"), "w", encoding="utf-8") as f:
            f.write("raise ModuleNotFoundError(\"No module named 'tomllib'\", name='tomllib')\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.shadow, ignore_errors=True)

    def test_the_shadow_really_hides_tomllib(self):  # covers: FIX-14
        r = subprocess.run([PY, "-c", "import tomllib"], capture_output=True, text=True,
                           env=no_tomllib_env(self.shadow))
        self.assertNotEqual(r.returncode, 0, "the test can't simulate Python without tomllib")

    def assertSaysOnceWithHandLines(self, r, where):
        out = told(r)
        self.assertEqual(r.returncode, 0, f"{where} failed:\n{out}")
        needs = [ln for ln in out.splitlines() if "3.11" in ln]
        self.assertEqual(len(needs), 1, f"{where} should say once that ruff's settings need Python 3.11:\n{out}")
        self.assertIn("ruff", needs[0].lower(), needs[0])
        self.assertNotIn("edited safely", out, f"{where} still reports files as not safely editable:\n{out}")
        given = hand_lines(out)
        self.assertTrue(given, f"{where} doesn't give the extend-exclude line to add by hand:\n{out}")
        for data in given:
            for entry in ENTRIES:
                self.assertIn(entry, data["extend-exclude"], f"{where}: the hand line misses {entry!r}:\n{out}")

    def test_init_says_once_and_gives_the_lines(self):  # covers: FIX-14
        for args in ((), ("--auto",)):
            with self.subTest(args=args):
                root = fresh_repo({"pyproject.toml": PYPROJECT, "ruff.toml": RUFF_TOML, "app.py": "print(1)\n"})
                self.addCleanup(shutil.rmtree, root, True)
                r = run_without_tomllib(self.shadow, root, "init", "--name", "Demo", *args, CLAUDE_PROJECT_DIR=root)
                self.assertTrue(os.path.isfile(os.path.join(root, ".regista", "config.json")), said(r))
                self.assertSaysOnceWithHandLines(r, f"init {' '.join(args)}")
                self.assertEqual(read_bytes(os.path.join(root, "pyproject.toml")), PYPROJECT.encode("utf-8"))
                self.assertEqual(read_bytes(os.path.join(root, "ruff.toml")), RUFF_TOML.encode("utf-8"))

    def test_init_without_ruff_settings_says_nothing_about_it(self):  # covers: FIX-14
        root = fresh_repo({"app.py": "print(1)\n"})
        self.addCleanup(shutil.rmtree, root, True)
        r = run_without_tomllib(self.shadow, root, "init", "--name", "Demo", CLAUDE_PROJECT_DIR=root)
        self.assertEqual(r.returncode, 0, said(r))
        self.assertNotIn("3.11", told(r))
        self.assertEqual(hand_lines(told(r)), [])

    def test_migrate_says_once_and_gives_the_lines(self):  # covers: FIX-14
        p = OldProject(gates=[{"name": "tests", "run": "echo ok", "when": ["commit"]}])
        self.addCleanup(p.cleanup)
        p.write("ruff.toml", RUFF_TOML)
        p.commit("pyproject.toml", PYPROJECT, "ruff settings")
        r = run_without_tomllib(self.shadow, p.root, "migrate")
        self.assertSaysOnceWithHandLines(r, "migrate")
        self.assertEqual(read_bytes(os.path.join(p.root, "pyproject.toml")), PYPROJECT.encode("utf-8"))
        self.assertEqual(read_bytes(os.path.join(p.root, "ruff.toml")), RUFF_TOML.encode("utf-8"))

    def test_readme_requirements_say_the_ruff_step_needs_3_11(self):  # covers: FIX-14
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            paragraphs = re.split(r"\n\s*\n", f.read())
        hits = [p for p in paragraphs if "3.10" in p and "3.11" in p and "ruff" in p.lower()]
        self.assertTrue(hits, "no README paragraph says Python 3.10 is enough except the ruff settings step (3.11)")


if __name__ == "__main__":
    unittest.main()
