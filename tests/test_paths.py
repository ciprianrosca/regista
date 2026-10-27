"""Paths inside the project folder are built by two functions, so the folder's name is one constant (NAME-2),
and the old name survives in scripts/ only where it has to (NAME-4).

NAME-2 is checked from outside, the way tests/test_names.py does it: the harness's folders are copied into a
temporary folder, only the constants block of the copy's scripts/regista.py is rewritten (this time PROJECT_DIR
too, to ".zephyr"), a fresh project is set up with the copy's `init --auto`, and the copy's hooks and commands
are driven. Everything must land under .zephyr/, and no .regista or .daedalus folder may appear anywhere.

NAME-4 is a static check of scripts/regista.py and scripts/regista. Since M2a the constants hold the Regista
names, and the old name (Daedalus) may stay only on an OLD_* constant line or a line whose comment says
"old name".

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_paths -v"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_names import (TOOL, PY, REPO, TODAY, as_agent, clean_env, decision, make_copy,  # noqa: E402
                        sh)

LAUNCHER = os.path.join(REPO, "scripts", "regista")

ZEPHYR_ALL = {
    "DISPLAY_NAME": "Zephyr",
    "SLUG": "zephyr",
    "PROJECT_DIR": ".zephyr",
    "ENV_PREFIX": "ZEPHYR_",
    "BRANCH_PREFIX": "zephyr/",
    "GATE_LOCK": "zephyr-gates.lock",
}

# The config defaults the spec names; each must sit inside the project folder.
PATH_DEFAULTS = ["status_file", "board_file", "state_dir", "memory_dir", "knowledge_file"]

DOC_FIRST_LINE = ("Core of the harness: state, gates and hook decisions for long-running builds in Claude Code "
                  "and Codex.")

STATS_SPEC = {"step": "stats", "milestone": "M1", "title": "Statistics", "requirements": [
    {"id": "STATS-1", "text": "WHEN the list is empty mean SHALL raise ValueError", "verify": "test"}]}

PLAN = """# Demo: plan

## M1 — Stats
Steps:
- [x] mean
- [ ] median

## M2 — CLI
Steps:
- [ ] calc command
"""

BIG = "".join(f"def f{i}(x):\n    return x + {i}\n" for i in range(10))  # 20 lines: needs a review


def path_functions(tool_path, calls):
    """Import the given regista.py in a clean subprocess and evaluate harness_path/harness_rel calls.
    `calls` maps a label to (function name, args); returns label -> result."""
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import regista; calls = json.loads(sys.argv[2]); "
            "print(json.dumps({k: getattr(regista, f)(*a) for k, (f, a) in calls.items()}))")
    r = subprocess.run([PY, "-c", code, os.path.dirname(tool_path), json.dumps(calls)], capture_output=True,
                       text=True, encoding="utf-8", env=clean_env(), cwd=os.path.dirname(tool_path))
    if r.returncode != 0:
        raise AssertionError("calling harness_path/harness_rel failed:\n" + r.stderr[-2000:])
    return json.loads(r.stdout)


def old_folders(top, names=(".daedalus",)):
    """Every folder under `top` with one of `names` (by default the old name, .daedalus)."""
    return [os.path.join(d, n) for d, dirs, _ in os.walk(top) for n in dirs if n in names]


def strings_in(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings_in(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings_in(v)


class PathFunctions(unittest.TestCase):
    """harness_path and harness_rel, in the repository as it is and in a copy with another folder name."""

    CALLS = {
        "folder": ["harness_path", ["/r"]],
        "last": ["harness_path", ["/r", "gates", "last.json"]],
        "nested": ["harness_path", ["/r", "fleet", "workers.json"]],
        "rel_folder": ["harness_rel", []],
        "rel_last": ["harness_rel", ["gates", "last.json"]],
        "rel_specs": ["harness_rel", ["specs"]],
        "rel_deep": ["harness_rel", ["reviews", "draft.json"]],
    }

    def expected(self, folder):
        return {
            "folder": os.path.join("/r", folder),
            "last": os.path.join("/r", folder, "gates", "last.json"),
            "nested": os.path.join("/r", folder, "fleet", "workers.json"),
            "rel_folder": folder,
            "rel_last": f"{folder}/gates/last.json",
            "rel_specs": f"{folder}/specs",
            "rel_deep": f"{folder}/reviews/draft.json",
        }

    def test_functions_give_todays_folder_in_the_repository(self):  # covers: NAME-2
        self.assertEqual(path_functions(TOOL, self.CALLS), self.expected(".regista"))

    def test_functions_follow_project_dir_in_a_renamed_copy(self):  # covers: NAME-2
        base = tempfile.mkdtemp(prefix="paths-test-")
        self.addCleanup(shutil.rmtree, base, True)
        tool_copy = make_copy(base, ZEPHYR_ALL)
        self.assertEqual(path_functions(tool_copy, self.CALLS), self.expected(".zephyr"))

    def test_unmodified_init_keeps_todays_defaults(self):  # covers: NAME-2, CODE-2
        base = tempfile.mkdtemp(prefix="paths-test-")
        self.addCleanup(shutil.rmtree, base, True)
        sh(base, "git", "init", "-q", "-b", "main")
        r = subprocess.run([PY, TOOL, "init", "--auto"], cwd=base, capture_output=True, text=True, encoding="utf-8",
                           env=clean_env())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(base, ".regista", "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual({k: cfg.get(k) for k in PATH_DEFAULTS},
                         {"status_file": ".regista/status.md", "board_file": ".regista/TODO.md",
                          "state_dir": ".regista/state", "memory_dir": ".regista/memory",
                          "knowledge_file": ".regista/knowledge.json"})
        self.assertEqual([s for s in strings_in(cfg) if ".daedalus" in s], [], "config values pointing into .daedalus/")
        self.assertEqual(old_folders(base), [], "init wrote a .daedalus folder")
        self.assertNotIn(".daedalus", r.stdout)


class RenamedProjectFolder(unittest.TestCase):
    """A copy whose constants block says PROJECT_DIR = ".zephyr": every record lands in .zephyr/."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="paths-test-")
        self.addCleanup(shutil.rmtree, self.base, True)
        copy = os.path.join(self.base, "harness")
        os.makedirs(copy)
        self.tool = make_copy(copy, ZEPHYR_ALL)
        self.root = os.path.join(self.base, "project")
        os.makedirs(self.root)
        sh(self.root, "git", "init", "-q", "-b", "main")
        sh(self.root, "git", "config", "user.email", "t@example.com")
        sh(self.root, "git", "config", "user.name", "T")
        self.write("app.py", 'print("hello")\n')
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "first commit")
        self.init = self.cli("init", "--auto")
        self.assertEqual(self.init.returncode, 0, self.init.stdout + self.init.stderr)
        self.cfg_path = os.path.join(self.root, ".zephyr", "config.json")
        if not os.path.exists(self.cfg_path):
            self.fail("init --auto in a copy with PROJECT_DIR=.zephyr didn't create .zephyr/config.json; "
                      f"folders now in the project: {sorted(os.listdir(self.root))}")
        with open(self.cfg_path, encoding="utf-8") as f:
            self.cfg = json.load(f)
        cfg = dict(self.cfg)
        cfg.update(gates=[{"name": "tests", "run": "echo ok", "when": ["commit"]}],
                   roles={**cfg.get("roles", {}), "source": ["src/**"], "tests": ["tests/**"]})
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "init")

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    # --- helpers ---

    def write(self, rel, text):
        fp = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)

    def commit(self, msg="work"):
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", msg)

    def cli(self, *args):
        return subprocess.run([PY, self.tool, *args], cwd=self.root, capture_output=True, text=True,
                              encoding="utf-8", env=clean_env())

    def hook(self, event, payload=None):
        data = {"cwd": self.root, **(payload or {})}
        r = subprocess.run([PY, self.tool, "hook", event], input=json.dumps(data), cwd=self.root,
                           capture_output=True, text=True, encoding="utf-8", env=clean_env())
        return json.loads(r.stdout) if r.stdout.strip() else None

    def commit_check(self):
        return self.hook("pre-tool", as_agent(None, "Bash", command="git add -A && git commit -m feat"))

    def zephyr(self, *parts):
        return os.path.join(self.root, ".zephyr", *parts)

    def assertNoOldFolder(self):
        found = old_folders(self.base, (".regista", ".daedalus"))
        self.assertEqual(found, [], "a .regista or .daedalus folder was written: " + ", ".join(
            os.path.relpath(p, self.base) for p in found))

    # --- the tests ---

    def test_init_creates_only_the_renamed_folder(self):  # covers: NAME-2
        self.assertTrue(os.path.isfile(self.zephyr(".gitignore")), "the folder's .gitignore should be in .zephyr/")
        self.assertTrue(os.path.isdir(self.zephyr("specs")))
        self.assertNotIn(".regista", self.init.stdout)
        self.assertNotIn(".daedalus", self.init.stdout)
        for key in PATH_DEFAULTS:
            self.assertIn(key, self.cfg, key)
            self.assertTrue(self.cfg[key].startswith(".zephyr/"), f"{key} = {self.cfg[key]!r}")
        stale = [s for s in strings_in(self.cfg) if ".regista" in s or ".daedalus" in s]
        self.assertEqual(stale, [], "config values still pointing into .regista/ or .daedalus/")
        self.assertNoOldFolder()

    def test_hooks_work_on_the_renamed_folder(self):  # covers: NAME-2
        out = self.hook("session-start", {"source": "startup"})
        self.assertTrue(out and out["hookSpecificOutput"].get("additionalContext"), out)
        refused = self.hook("pre-tool", as_agent(None, "Write", file_path=os.path.join(self.root, ".env"),
                                                 content="x"))
        self.assertEqual(decision(refused), "deny")
        # the harness's own records are guarded in their new place
        self.assertEqual(decision(self.hook("pre-tool", as_agent(
            "zephyr:builder", "Write", file_path=self.zephyr("gates", "requirements.json"), content="{}"))), "deny")
        self.assertEqual(decision(self.hook("pre-tool", as_agent(None, "Edit", file_path=self.cfg_path,
                                                                 old_string="a", new_string="b"))), "ask")
        self.write("src/a.py", "x = 1\n")
        self.commit("feat: a")
        out = self.hook("stop", {"stop_hook_active": False})
        self.assertEqual((out or {}).get("decision"), "block", out)
        self.assertNoOldFolder()

    def test_gate_run_writes_its_results_under_the_renamed_folder(self):  # covers: NAME-2
        self.write(".zephyr/specs/stats.json", json.dumps(STATS_SPEC))
        self.write("tests/test_stats.py", "# covers: STATS-1\n")
        self.commit("tests")
        r = self.cli("gate")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isfile(self.zephyr("gates", "last.json")),
                        "the gate run should write .zephyr/gates/last.json")
        with open(self.zephyr("gates", "requirements.json"), encoding="utf-8") as f:
            self.assertTrue(json.load(f)["STATS-1"]["passes"])
        self.assertNoOldFolder()

    def test_requirements_reads_specs_from_the_renamed_folder(self):  # covers: NAME-2
        self.write(".zephyr/specs/stats.json", json.dumps(STATS_SPEC))
        r = self.cli("requirements")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("STATS-1", r.stdout)
        self.assertNoOldFolder()

    def test_review_record_reads_and_writes_the_renamed_folder(self):  # covers: NAME-2
        self.write("src/a.py", BIG)
        out = self.commit_check()
        self.assertEqual(decision(out), "deny", out)
        self.assertIn("no review", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.write(".zephyr/reviews/draft.json", json.dumps(
            {"verdict": "approve", "summary": "s", "checked": ["spec", "edge cases"], "findings": []}))
        r = self.cli("review-record")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNone(decision(self.commit_check()))
        self.assertNoOldFolder()

    def test_build_all_run_is_recorded_in_the_renamed_folder(self):  # covers: NAME-2
        self.write(".zephyr/plan.md", PLAN)
        self.commit("docs: plan")
        r = self.cli("run", "start")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isfile(self.zephyr("run.json")), "run start should write .zephyr/run.json")
        with open(self.zephyr("run.json"), encoding="utf-8") as f:
            self.assertTrue(json.load(f)["active"])
        self.assertIn("active", self.cli("run").stdout)
        out = self.hook("stop", {})
        self.assertEqual((out or {}).get("decision"), "block", out)
        self.assertIn("build-all run is active", out["reason"])
        self.assertNoOldFolder()


def name_lines(path, word):
    """(line number, text) for each line of `path` that contains `word` in any case."""
    with open(path, encoding="utf-8") as f:
        return [(n, line.rstrip("\n")) for n, line in enumerate(f, 1) if word in line.lower()]


def old_name_lines(path):
    """(line number, text) for each line of `path` that contains "daedalus" in any case."""
    return name_lines(path, "daedalus")


def comments_by_line(path):
    """line number -> the text of the comment on that line (Python files)."""
    with open(path, encoding="utf-8") as f:
        source = f.read()
    return {tok.start[0]: tok.string for tok in tokenize.generate_tokens(io.StringIO(source).readline)
            if tok.type == tokenize.COMMENT}


CONSTANT_LINE = re.compile(r'^(%s) = "[^"\n]*"[ \t]*(#.*)?$' % "|".join(TODAY))
OLD_CONSTANT_LINE = re.compile(r"^OLD_[A-Z0-9_]+ = ")


class OldNameInScripts(unittest.TestCase):
    """NAME-4 after the rename: the old name stays only on OLD_* constant lines and on lines whose comment says
    "old name"; the six constants hold the Regista names."""

    def test_tool_names_the_old_name_only_on_old_constants_or_old_name_comments(self):  # covers: NAME-4, CODE-3
        comments = comments_by_line(TOOL)
        offending = [(n, text) for n, text in old_name_lines(TOOL)
                     if not OLD_CONSTANT_LINE.match(text) and "old name" not in comments.get(n, "").lower()]
        if offending:
            self.fail("scripts/regista.py lines that still name Daedalus:\n" + "\n".join(
                f"  line {n}: {text.strip()}" for n, text in offending))

    def test_constants_block_lines_are_exactly_six(self):  # covers: NAME-4
        block = [n for n, text in name_lines(TOOL, "regista") if CONSTANT_LINE.match(text)]
        self.assertEqual(len(block), len(TODAY), f"constants-block lines naming Regista: {block}")
        old = [n for n, text in old_name_lines(TOOL) if CONSTANT_LINE.match(text)]
        self.assertEqual(old, [], "constants-block lines still naming Daedalus")

    def test_launcher_names_the_harness_on_one_line(self):  # covers: NAME-4, CODE-1
        old = old_name_lines(LAUNCHER)
        self.assertEqual(old, [], "scripts/regista lines that still name Daedalus:\n" + "\n".join(
            f"  line {n}: {text.strip()}" for n, text in old))
        with open(LAUNCHER, encoding="utf-8") as f:
            names = [line for line in f if re.match(r"""^\s*name=["']?Regista["']?\s*(#.*)?$""", line)]
        self.assertEqual(len(names), 1, "scripts/regista should have exactly one name=Regista line")
        with open(LAUNCHER, encoding="utf-8") as f:
            self.assertIn("regista.py", f.read(), "the launcher should run regista.py")

    def test_module_docstring_starts_with_the_new_line(self):  # covers: NAME-4
        with open(TOOL, encoding="utf-8") as f:
            head = f.read(300)
        self.assertIn('"""' + DOC_FIRST_LINE + "\n", head)
        r = subprocess.run([PY, TOOL, "--help"], capture_output=True, text=True, encoding="utf-8", env=clean_env(),
                           cwd=tempfile.gettempdir())
        self.assertIn(" ".join(DOC_FIRST_LINE.split()), " ".join(r.stdout.split()))


if __name__ == "__main__":
    unittest.main()
