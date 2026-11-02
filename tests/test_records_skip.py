"""Commits that change only Regista's records skip the gates by default, a nested git worktree or repository in
the working tree doesn't count as part of a commit that names its paths, and a commit that stages everything is
refused while such a folder is untracked.

A gate whose command fails shows whether the gates ran: a commit the hook allows despite it skipped them.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_records_skip -v"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# test_regista points HOME and CODEX_HOME at a temporary folder when it is imported.
from test_regista import PY, TOOL, Project, bash, decision, regista, sh  # noqa: E402

FAILING = [{"name": "tests", "run": "echo boom; exit 1", "when": ["commit"]}]
PLANNED = {"step": "x", "milestone": "M1", "title": "Something later", "state": "planned",
           "requirements": [{"id": "X-1", "text": "WHEN asked the tool SHALL answer", "verify": "test"}]}
WORKTREES = ".claude/worktrees/"
RECORDS_BY_NAME = "git add .regista/TODO.md .regista/specs/x.json && git commit -m records"


def reason(out):
    return (out or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason", "")


def ignore_lines(root):
    path = os.path.join(root, ".gitignore")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f.read().splitlines()]


def set_worktrees_ignored(p, ignored):
    """Add or remove the .claude/worktrees/ line in the project's .gitignore and commit the result directly
    (not through the hook), so the .gitignore itself isn't part of the change under test."""
    path = os.path.join(p.root, ".gitignore")
    lines = ignore_lines(p.root) or []
    lines = [line for line in lines if line.rstrip("/") != WORKTREES.rstrip("/")]
    if ignored:
        lines.append(WORKTREES)
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(line + "\n" for line in lines))
    sh(p.root, "git", "add", ".gitignore")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=p.root).returncode:
        sh(p.root, "git", "commit", "-q", "-m", "gitignore")


def write_records(p):
    p.write(".regista/TODO.md", "# Board\n\n- [ ] later\n")
    p.write(".regista/specs/x.json", json.dumps(PLANNED))


def add_worktree(p):
    sh(p.root, "git", "worktree", "add", "-q", "-b", "w1", ".claude/worktrees/w1")
    assert os.path.isfile(os.path.join(p.root, ".claude", "worktrees", "w1", ".git"))
    p.write(".claude/worktrees/w1/src/b.py", "y = 2\n")  # code inside the worktree isn't this commit's


class InitDefaults(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="regista-records-init-")
        sh(self.root, "git", "init", "-q", "-b", "main")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def init(self, *args):
        r = subprocess.run([PY, TOOL, "init", *args], cwd=self.root, capture_output=True, text=True,
                           encoding="utf-8", env={**os.environ, "CLAUDE_PROJECT_DIR": self.root})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return regista.load_json(os.path.join(self.root, ".regista", "config.json"), {})

    def auto(self):
        with open(os.path.join(self.root, "go.mod"), "w", encoding="utf-8") as f:
            f.write("module x\n")
        return self.init("--auto", "--name", "X")

    def write_gitignore(self, text):
        with open(os.path.join(self.root, ".gitignore"), "w", encoding="utf-8") as f:
            f.write(text)

    def test_init_skips_gates_for_records_and_docs(self):  # covers: FIX-1
        cfg = self.init("--name", "X")
        self.assertIn(".regista/**", cfg.get("no_gates_for", []))
        self.assertIn("docs/**", cfg.get("no_gates_for", []))

    def test_init_auto_skips_gates_for_records_and_docs(self):  # covers: FIX-1
        cfg = self.auto()
        self.assertIn(".regista/**", cfg.get("no_gates_for", []))
        self.assertIn("docs/**", cfg.get("no_gates_for", []))

    def test_init_creates_gitignore_with_worktrees(self):  # covers: FIX-8
        self.init("--name", "X")
        self.assertEqual((ignore_lines(self.root) or []).count(WORKTREES), 1, ignore_lines(self.root))

    def test_init_keeps_existing_gitignore_lines(self):  # covers: FIX-8
        self.write_gitignore("node_modules/\n*.log")  # no newline at the end
        self.init("--name", "X")
        lines = ignore_lines(self.root) or []
        self.assertIn("node_modules/", lines)
        self.assertIn("*.log", lines)
        self.assertEqual(lines.count(WORKTREES), 1, lines)

    def test_init_auto_creates_gitignore_with_worktrees(self):  # covers: FIX-8
        self.auto()
        self.assertEqual((ignore_lines(self.root) or []).count(WORKTREES), 1, ignore_lines(self.root))

    def test_init_auto_keeps_existing_gitignore_lines(self):  # covers: FIX-8
        self.write_gitignore("build/\n")
        self.auto()
        lines = ignore_lines(self.root) or []
        self.assertIn("build/", lines)
        self.assertEqual(lines.count(WORKTREES), 1, lines)

    def test_init_doesnt_duplicate_worktrees_line(self):  # covers: FIX-8
        # init refuses to run twice on one project, so the second run is a project that already has the line
        self.write_gitignore("build/\n.claude/worktrees/\n")
        self.init("--name", "X")
        lines = ignore_lines(self.root)
        self.assertEqual(lines.count(WORKTREES), 1, lines)
        self.assertIn("build/", lines)

    def test_init_auto_doesnt_duplicate_worktrees_line(self):  # covers: FIX-8
        self.write_gitignore(".claude/worktrees/\n")
        self.auto()
        self.assertEqual(ignore_lines(self.root).count(WORKTREES), 1, ignore_lines(self.root))


class RecordsCommit(unittest.TestCase):
    def tearDown(self):
        self.p.cleanup()

    def commit(self):
        return self.p.hook("pre-tool", bash("git add -A && git commit -m records"))

    def test_records_only_commit_skips_gates_by_default(self):  # covers: FIX-1
        # no_gates_for is left as `init` wrote it
        self.p = Project(gates=FAILING)
        write_records(self.p)
        out = self.commit()
        self.assertNotEqual(decision(out), "deny", reason(out))
        self.assertNotIn("FAIL", reason(out))

    def test_config_change_still_runs_gates(self):  # covers: FIX-1
        self.p = Project(gates=FAILING)
        cfgfile = os.path.join(self.p.root, ".regista", "config.json")
        c = regista.load_json(cfgfile, {})
        c["gates"].append({"name": "lint", "run": "echo lint"})  # adding a check: allowed, but the gates run
        regista.save_json(cfgfile, c)
        out = self.commit()
        self.assertEqual(decision(out), "deny", out)
        self.assertIn("tests: FAIL", reason(out))


class NestedRepositories(unittest.TestCase):
    """An untracked folder holding a git worktree or repository of its own is not part of a commit that names
    its paths; an untracked symlink always is."""

    def setUp(self):
        # set explicitly, so these tests don't depend on what `init` writes
        self.p = Project(gates=FAILING, no_gates_for=["docs/**", ".regista/**"])
        set_worktrees_ignored(self.p, False)  # `init` ignores .claude/worktrees/; these tests need it untracked
        self.outside = None

    def tearDown(self):
        self.p.cleanup()
        if self.outside:
            shutil.rmtree(self.outside, ignore_errors=True)

    def check(self, nested):
        status = sh(self.p.root, "git", "status", "--porcelain", "--untracked-files=all")
        self.assertIn(nested, status)  # the nested folder really shows up as untracked
        write_records(self.p)
        out = self.p.hook("pre-tool", bash(RECORDS_BY_NAME))
        self.assertNotEqual(decision(out), "deny", reason(out))  # records only: the gates are skipped
        self.assertNotIn("FAIL", reason(out))
        sh(self.p.root, "git", "add", ".regista/TODO.md", ".regista/specs/x.json")
        out = self.p.hook("pre-tool", bash("git commit -m records"))  # already staged, then a bare commit
        self.assertNotEqual(decision(out), "deny", reason(out))
        self.assertNotIn("FAIL", reason(out))
        self.p.write("src/a.py", "x = 1\n")
        out = self.p.hook("pre-tool", bash("git add src/a.py && git commit -m work"))
        self.assertEqual(decision(out), "deny", out)  # code: the failing gate ran
        self.assertIn("tests: FAIL", reason(out))
        self.p.write("README.md", "changed\n")
        out = self.p.hook("pre-tool", bash("git commit -am work"))
        self.assertEqual(decision(out), "deny", out)
        self.assertIn("tests: FAIL", reason(out))

    def test_git_worktree_folder_doesnt_count(self):  # covers: FIX-2
        add_worktree(self.p)
        self.check(".claude/")

    def test_nested_git_repository_doesnt_count(self):  # covers: FIX-2
        nested = os.path.join(self.p.root, "scratch", "other")
        os.makedirs(nested)
        sh(nested, "git", "init", "-q")
        self.assertTrue(os.path.isdir(os.path.join(nested, ".git")))
        self.p.write("scratch/other/main.py", "print('hi')\n")
        self.check("scratch/")

    def test_symlink_to_nested_repository_counts(self):  # covers: FIX-2
        self.outside = tempfile.mkdtemp(prefix="regista-records-outside-")
        sh(self.outside, "git", "init", "-q")
        with open(os.path.join(self.outside, "main.py"), "w", encoding="utf-8") as f:
            f.write("print('hi')\n")
        os.makedirs(os.path.join(self.p.root, "vendor"))
        os.symlink(self.outside, os.path.join(self.p.root, "vendor", "lib"))
        status = sh(self.p.root, "git", "status", "--porcelain", "--untracked-files=all")
        self.assertIn("vendor/lib", status)
        write_records(self.p)
        out = self.p.hook("pre-tool", bash(RECORDS_BY_NAME))
        self.assertEqual(decision(out), "deny", out)  # the symlink is part of the change: the failing gate ran
        self.assertIn("tests: FAIL", reason(out))


class StageEverything(unittest.TestCase):
    """A commit that stages every untracked file is refused while an untracked nested worktree exists."""

    def setUp(self):
        self.p = Project(gates=FAILING, no_gates_for=["docs/**", ".regista/**"])
        set_worktrees_ignored(self.p, False)
        add_worktree(self.p)
        write_records(self.p)  # records only, so a refusal can't come from the failing gate

    def tearDown(self):
        self.p.cleanup()

    def test_stage_everything_next_to_worktree_is_refused(self):  # covers: FIX-8
        for add in ("git add -A", "git add --all", "git add .", "git add .claude"):
            with self.subTest(add=add):
                out = self.p.hook("pre-tool", bash(f"{add} && git commit -m records"))
                self.assertEqual(decision(out), "deny", out)
                self.assertIn(".claude/worktrees/w1", reason(out))  # names the folder
                self.assertIn(".gitignore", reason(out))  # and says how to fix it
                self.assertNotIn("tests: FAIL", reason(out))  # refused before (or instead of) the gates

    def test_ignored_worktree_lets_stage_everything_through(self):  # covers: FIX-8
        set_worktrees_ignored(self.p, True)
        cmd = "git add -A && git commit -m records"
        out = self.p.hook("pre-tool", bash(cmd))
        self.assertNotEqual(decision(out), "deny", reason(out))
        self.assertNotIn("FAIL", reason(out))
        subprocess.run(cmd, shell=True, cwd=self.p.root, check=True, capture_output=True)
        shown = sh(self.p.root, "git", "show", "--stat=400", "--format=", "HEAD")
        self.assertIn(".regista/TODO.md", shown)
        self.assertNotIn(".claude/worktrees", shown)
        tree = sh(self.p.root, "git", "ls-tree", "-r", "HEAD")
        self.assertNotIn("160000", tree)  # no gitlink anywhere in history's latest tree


if __name__ == "__main__":
    unittest.main()
