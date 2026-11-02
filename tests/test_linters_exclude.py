"""Linters stay out of Regista's folders.

FIX-3: `regista.py init` (with or without --auto) and `regista.py migrate` add ".regista" and ".claude/worktrees" to
ruff's `extend-exclude` when the project has ruff settings (pyproject.toml with [tool.ruff], ruff.toml or
.ruff.toml), keep every other line as it was, say so, and change nothing on a second run.

FIX-9: `regista.py doctor` warns, naming the gate, when .regista/ holds code files and a gate runs a linter that
would check them: eslint, flake8, pylint, biome, prettier --check, and ruff when its settings don't already list both
folders in `exclude` or `extend-exclude`.

ruff isn't installed here: the tests check the files written, not ruff's behaviour.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_linters_exclude -v"""
import difflib
import os
import shutil
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# test_regista points HOME and CODEX_HOME at a temporary folder when it is imported.
from test_regista import Project, clean_env, sh  # noqa: E402
from test_migration import OldProject, migrate, read_bytes, run_tool, said  # noqa: E402

ENTRIES = [".regista", ".claude/worktrees"]

PYPROJECT = """\
# The project's settings. Keep this comment.
[project]
name = "demo"
version = "0.1.0"
dependencies = ["requests>=2"]

[tool.ruff]
# ruff reads this table
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F"]  # errors and pyflakes

[tool.black]
line-length = 100
"""

PYPROJECT_WITH_LIST = """\
[project]
name = "demo"

[tool.ruff]
line-length = 120
extend-exclude = ["build", ".regista"]  # generated and harness files

[tool.ruff.lint]
select = ["E"]
"""

PYPROJECT_DONE = """\
[project]
name = "demo"

[tool.ruff]
line-length = 120
extend-exclude = [".regista", ".claude/worktrees", "build"]
"""

PYPROJECT_NO_RUFF = """\
[project]
name = "demo"

[tool.black]
line-length = 100
"""

RUFF_TOML = """\
# ruff settings at the root
line-length = 100
exclude = ["vendor"]

[lint]
select = ["E", "F"]

[format]
quote-style = "double"
"""


# --- helpers --------------------------------------------------------------------------------------------------

def fresh_repo(files):
    """A temporary git repository holding `files` (relative path to text), without a .regista/ folder yet."""
    root = tempfile.mkdtemp(prefix="regista-lint-")
    sh(root, "git", "init", "-q", "-b", "main")
    sh(root, "git", "config", "user.email", "t@example.com")
    sh(root, "git", "config", "user.name", "T")
    for rel, text in files.items():
        write_bytes(os.path.join(root, *rel.split("/")), text.encode("utf-8"))
    sh(root, "git", "add", "-A")
    sh(root, "git", "commit", "-q", "-m", "first")
    return root


def write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def init(root, *args):
    r = run_tool(root, "init", "--name", "Demo", *args, env=clean_env(CLAUDE_PROJECT_DIR=root))
    if r.returncode != 0 or not os.path.isfile(os.path.join(root, ".regista", "config.json")):
        raise AssertionError("init failed:\n" + r.stdout + r.stderr)
    return r


def ruff_lines(out):
    """The lines of a command's output that say what it did to ruff's settings."""
    return [ln for ln in out.splitlines() if "ruff" in ln.lower() and ".claude/worktrees" in ln]


class RuffExcludeChecks:
    """Assertions shared by the FIX-3 tests (a mixin for unittest.TestCase)."""

    def assertExcludes(self, data, where):
        listed = data.get("extend-exclude")
        self.assertIsInstance(listed, list, f"no extend-exclude list in {where}: {data}")
        for entry in ENTRIES:
            self.assertEqual(listed.count(entry), 1, f"{entry!r} should be listed once in {where}: {listed}")
        return listed

    def assertOnlyExcludeChanged(self, before, after):
        """Every line of `before` (with its line ending) is still in `after`, in order, except the extend-exclude
        line it may have rewritten."""
        old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
        for tag, i1, i2, _, _ in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
            if tag in ("replace", "delete"):
                for line in old[i1:i2]:
                    self.assertIn("extend-exclude", line, f"a line other than extend-exclude changed: {line!r}\n"
                                                          f"--- after ---\n{after}")

    def assertSaid(self, r, where):
        out = r.stdout + r.stderr
        self.assertTrue(ruff_lines(out), f"the output doesn't say ruff now skips Regista's folders ({where}):\n{out}")


# --- FIX-3: init, init --auto and migrate add the excludes ----------------------------------------------------------

class InitAddsRuffExcludes(RuffExcludeChecks, unittest.TestCase):

    def make(self, files):
        root = fresh_repo(files)
        self.addCleanup(shutil.rmtree, root, True)
        return root

    def read(self, root, rel):
        with open(os.path.join(root, rel), encoding="utf-8", newline="") as f:
            return f.read()

    def test_init_adds_the_key_to_pyproject_tool_ruff(self):  # covers: FIX-3
        for args in ((), ("--auto",)):
            with self.subTest(args=args):
                root = self.make({"pyproject.toml": PYPROJECT, "app.py": "print(1)\n"})
                r = init(root, *args)
                after = self.read(root, "pyproject.toml")
                data = tomllib.loads(after)
                listed = self.assertExcludes(data["tool"]["ruff"], "[tool.ruff]")
                self.assertEqual(sorted(listed), sorted(ENTRIES))
                self.assertNotIn("extend-exclude", data["tool"]["ruff"]["lint"], "the key went under [tool.ruff.lint]")
                self.assertNotIn("extend-exclude", data["tool"]["black"], "the key went under [tool.black]")
                self.assertNotIn("extend-exclude", data["project"])
                self.assertOnlyExcludeChanged(PYPROJECT, after)
                self.assertSaid(r, f"init {' '.join(args)}")

    def test_init_adds_to_an_existing_list_without_duplicates(self):  # covers: FIX-3
        for args in ((), ("--auto",)):
            with self.subTest(args=args):
                root = self.make({"pyproject.toml": PYPROJECT_WITH_LIST, "app.py": "print(1)\n"})
                r = init(root, *args)
                after = self.read(root, "pyproject.toml")
                data = tomllib.loads(after)
                listed = self.assertExcludes(data["tool"]["ruff"], "[tool.ruff]")
                self.assertIn("build", listed)
                self.assertEqual(len(listed), 3, listed)
                self.assertEqual(after.count("extend-exclude"), 1, after)
                self.assertEqual(data["tool"]["ruff"]["line-length"], 120)
                self.assertEqual(data["tool"]["ruff"]["lint"], {"select": ["E"]})
                self.assertOnlyExcludeChanged(PYPROJECT_WITH_LIST, after)
                self.assertSaid(r, f"init {' '.join(args)}")

    def test_init_edits_ruff_toml_and_dot_ruff_toml_at_the_top_level(self):  # covers: FIX-3
        for name in ("ruff.toml", ".ruff.toml"):
            with self.subTest(file=name):
                root = self.make({name: RUFF_TOML, "app.py": "print(1)\n"})
                r = init(root)
                after = self.read(root, name)
                data = tomllib.loads(after)
                self.assertExcludes(data, f"{name} (top level)")
                self.assertEqual(data["exclude"], ["vendor"], "the existing exclude list changed")
                self.assertNotIn("extend-exclude", data["lint"], "the key went under [lint]")
                self.assertNotIn("extend-exclude", data["format"], "the key went under [format]")
                self.assertNotIn("tool", data, "ruff.toml has no [tool.ruff] header")
                self.assertOnlyExcludeChanged(RUFF_TOML, after)
                self.assertFalse(os.path.exists(os.path.join(root, "pyproject.toml")), "a pyproject.toml was created")
                self.assertSaid(r, name)

    def test_init_keeps_windows_line_endings(self):  # covers: FIX-3
        crlf = PYPROJECT.replace("\n", "\r\n")
        root = self.make({"pyproject.toml": crlf})
        init(root)
        after = read_bytes(os.path.join(root, "pyproject.toml")).decode("utf-8")
        self.assertExcludes(tomllib.loads(after)["tool"]["ruff"], "[tool.ruff]")
        self.assertOnlyExcludeChanged(crlf, after)
        self.assertEqual(after.count("\n"), after.count("\r\n"), "lines ending in a bare newline were added")

    def test_settings_that_already_list_both_entries_are_left_byte_identical(self):  # covers: FIX-3
        for args in ((), ("--auto",)):
            with self.subTest(args=args):
                root = self.make({"pyproject.toml": PYPROJECT_DONE, "app.py": "print(1)\n"})
                init(root, *args)
                self.assertEqual(read_bytes(os.path.join(root, "pyproject.toml")), PYPROJECT_DONE.encode("utf-8"))

    def test_a_project_without_ruff_settings_gets_none(self):  # covers: FIX-3
        for args in ((), ("--auto",)):
            with self.subTest(args=args, pyproject=False):
                root = self.make({"app.py": "print(1)\n"})
                r = init(root, *args)
                for name in ("pyproject.toml", "ruff.toml", ".ruff.toml"):
                    self.assertFalse(os.path.exists(os.path.join(root, name)), f"init created {name}")
                self.assertEqual(ruff_lines(r.stdout + r.stderr), [])
            with self.subTest(args=args, pyproject=True):
                root = self.make({"pyproject.toml": PYPROJECT_NO_RUFF, "app.py": "print(1)\n"})
                r = init(root, *args)
                self.assertEqual(read_bytes(os.path.join(root, "pyproject.toml")), PYPROJECT_NO_RUFF.encode("utf-8"))
                for name in ("ruff.toml", ".ruff.toml"):
                    self.assertFalse(os.path.exists(os.path.join(root, name)), f"init created {name}")
                self.assertEqual(ruff_lines(r.stdout + r.stderr), [])


class MigrateAddsRuffExcludes(RuffExcludeChecks, unittest.TestCase):

    def old(self, rel, text):
        p = OldProject(gates=[{"name": "tests", "run": "echo ok", "when": ["commit"]}])
        self.addCleanup(p.cleanup)
        p.commit(rel, text, "ruff settings")
        return p

    def test_migrate_adds_the_excludes_and_a_second_run_changes_nothing(self):  # covers: FIX-3
        for rel, text in (("pyproject.toml", PYPROJECT), (".ruff.toml", RUFF_TOML)):
            with self.subTest(file=rel):
                p = self.old(rel, text)
                path = os.path.join(p.root, rel)
                r = migrate(p.root)
                self.assertEqual(r.returncode, 0, said(r))
                after = read_bytes(path).decode("utf-8")
                data = tomllib.loads(after)
                self.assertExcludes(data["tool"]["ruff"] if rel == "pyproject.toml" else data, rel)
                self.assertNotIn(".daedalus", after)
                self.assertOnlyExcludeChanged(text, after)
                self.assertSaid(r, f"migrate, {rel}")
                again = migrate(p.root)
                self.assertEqual(again.returncode, 0, said(again))
                self.assertEqual(read_bytes(path), after.encode("utf-8"), "a second migrate changed the ruff settings")

    def test_migrate_without_ruff_settings_adds_none(self):  # covers: FIX-3
        p = self.old("pyproject.toml", PYPROJECT_NO_RUFF)
        r = migrate(p.root)
        self.assertEqual(r.returncode, 0, said(r))
        self.assertEqual(read_bytes(os.path.join(p.root, "pyproject.toml")), PYPROJECT_NO_RUFF.encode("utf-8"))
        for name in ("ruff.toml", ".ruff.toml"):
            self.assertFalse(os.path.exists(os.path.join(p.root, name)), f"migrate created {name}")
        self.assertEqual(ruff_lines(r.stdout + r.stderr), [])


# --- FIX-9: doctor warns about linters Regista doesn't configure ----------------------------------------------------

class DoctorWarnsAboutOtherLinters(unittest.TestCase):

    def doctor(self, gates, files):
        p = Project(gates=gates)
        self.addCleanup(p.cleanup)
        for rel, text in files.items():
            p.write(rel, text)
        r = p.cli("doctor")
        return r.stdout + r.stderr

    def warnings_for(self, out, gate):
        return [ln for ln in out.splitlines() if gate in ln and ".regista/" in ln and ".claude/worktrees" in ln]

    def test_an_eslint_gate_with_code_under_regista_is_named_in_a_warning(self):  # covers: FIX-9
        out = self.doctor([{"name": "webcheck", "run": "npx eslint .", "when": ["commit"]}],
                          {".regista/research/probe.js": "console.log(1)\n"})
        lines = self.warnings_for(out, "webcheck")
        self.assertEqual(len(lines), 1, out)
        self.assertIn("exclude", lines[0].lower())

    def test_other_unconfigured_linters_and_code_kinds_warn_too(self):  # covers: FIX-9
        for run, rel in (("flake8 src", ".regista/research/a.py"), ("pylint src", ".regista/tools/b.py"),
                         ("npx biome check .", ".regista/research/c.ts"),
                         ("npx prettier --check .", ".regista/research/d.mjs")):
            with self.subTest(run=run, file=rel):
                out = self.doctor([{"name": "stylecheck", "run": run, "when": ["commit"]}], {rel: "x = 1\n"})
                self.assertEqual(len(self.warnings_for(out, "stylecheck")), 1, out)

    def test_no_code_files_under_regista_means_no_warning(self):  # covers: FIX-9
        out = self.doctor([{"name": "webcheck", "run": "npx eslint .", "when": ["commit"]}],
                          {".regista/research/notes.md": "# notes\n", ".regista/research/data.json": "{}\n",
                           "src/app.js": "console.log(1)\n"})
        self.assertEqual(self.warnings_for(out, "webcheck"), [], out)

    def test_a_ruff_gate_whose_settings_dont_exclude_both_folders_warns(self):  # covers: FIX-9
        # a project set up before init edited ruff's settings: one folder excluded, or no exclude at all
        for rel, text in (("pyproject.toml", '[project]\nname = "demo"\n\n[tool.ruff]\nline-length = 100\n'),
                          ("ruff.toml", 'extend-exclude = [".regista"]\n')):
            with self.subTest(file=rel):
                out = self.doctor([{"name": "pycheck", "run": "uv run ruff check .", "when": ["commit"]}],
                                  {rel: text, ".regista/research/probe.py": "print(1)\n"})
                lines = self.warnings_for(out, "pycheck")
                self.assertEqual(len(lines), 1, out)
                self.assertIn("exclude", lines[0].lower())

    def test_a_ruff_gate_whose_settings_exclude_both_folders_doesnt_warn(self):  # covers: FIX-9
        for rel, text in (("pyproject.toml", '[tool.ruff]\nextend-exclude = [".regista", ".claude/worktrees"]\n'),
                          (".ruff.toml", 'exclude = ["build", ".regista", ".claude/worktrees"]\n')):
            with self.subTest(file=rel):
                out = self.doctor([{"name": "pycheck", "run": "uv run ruff check .", "when": ["commit"]}],
                                  {rel: text, ".regista/research/probe.py": "print(1)\n",
                                   ".regista/research/probe.js": "1\n"})
                self.assertEqual([ln for ln in out.splitlines() if "pycheck" in ln and ".claude/worktrees" in ln],
                                 [], out)

if __name__ == "__main__":
    unittest.main()
