"""A worker's own records (hooks log, gate history, compactions, reviews it never committed) are kept in the main
project at .regista/fleet/logs/<milestone>/<worker>/ when its worktree is removed without a merge; and when they
can't be copied there, the worktree stays and the command says why. Driven like test_regista's Fleet tests:
stand-ins for `claude` and `tmux`, the real `regista.py fleet` commands."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_regista import FAKE, FLEET_PLAN, PY, TOOL, Project, bash, jload, sh  # noqa: E402

RECORDS = {
    "hooks.log": '{"at": "2026-10-28T09:00:00", "tool": "Bash", "reason": "refusé"}\n'.encode("utf-8"),
    "gates/history.jsonl": b'{"at": "2026-10-28T09:01:00", "gate": "tests", "ok": true}\n',
    "compactions.jsonl": b'{"at": "2026-10-28T09:02:00", "trigger": "auto"}\n',
    "reviews/abandoned1.json": b'{"verdict": "changes", "note": "no trailing newline"}',
}


class WorkerLogsWithoutMerge(unittest.TestCase):
    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "test ! -f broken.txt", "when": ["commit"]}],
                         review={"required_for": []}, requirements={"enforce_coverage": False,
                                                                     "milestone_needs_e2e": False})
        self.addCleanup(self.p.cleanup)
        self.p.commit(".regista/plan.md", FLEET_PLAN, "plan")
        self.fake = tempfile.mkdtemp(prefix="regista-fake-")
        self.addCleanup(shutil.rmtree, self.fake, True)
        for tool in ("claude", "tmux"):
            path = os.path.join(self.fake, tool)
            with open(path, "w", encoding="utf-8") as f:
                f.write(FAKE.replace("PYTHON", sys.executable))
            os.chmod(path, 0o755)
        self.env = {"REGISTA_CLAUDE": os.path.join(self.fake, "claude"), "REGISTA_TMUX": os.path.join(self.fake, "tmux"),
                    "FAKE_DIR": self.fake, "REGISTA_LAUNCH_WAIT": "3", "CLAUDE_CODE_SESSION_ID": "overseer-1"}
        self.wt = os.path.join(self.p.root, ".regista", "worktrees", "m2")
        self.kept = os.path.join(self.p.root, ".regista", "fleet", "logs", "M2", "m2")

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.p.root, capture_output=True)

    def fleet(self, *args):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        env.update(self.env)
        return subprocess.run([PY, TOOL, "fleet", *args], cwd=self.p.root, capture_output=True, text=True,
                              encoding="utf-8", env=env)

    def spawn(self):
        self.p.hook("pre-tool", {**bash(f'python3 "{TOOL}" fleet spawn M2'), "session_id": "overseer-1",
                                 "permission_mode": "acceptEdits"}, extra_env=self.env)
        r = self.fleet("spawn", "M2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # read now: the worker's entry may be gone once it is removed
        self.session_id = jload(os.path.join(self.p.root, ".regista", "fleet", "workers.json"))["m2"]["session_id"]
        self.assertTrue(self.session_id)

    def worker_commit(self, rel, text):
        fp = os.path.join(self.wt, *rel.split("/"))
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "feat: work")

    def write_records(self):
        for rel, data in RECORDS.items():
            fp = os.path.join(self.wt, ".regista", *rel.split("/"))
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "wb") as f:
                f.write(data)

    def assert_kept(self):
        for rel, data in RECORDS.items():
            fp = os.path.join(self.kept, *rel.split("/"))
            self.assertTrue(os.path.isfile(fp), f"{rel} wasn't kept under .regista/fleet/logs/M2/m2/")
            with open(fp, "rb") as f:
                self.assertEqual(f.read(), data, f"{rel} changed on the way")
        index = jload(os.path.join(self.kept, "index.json"))
        self.assertEqual(index.get("session_id"), self.session_id)
        values = [v for v in index.values() if isinstance(v, str)]
        for want in ("m2", "M2", "regista/m2"):
            self.assertIn(want, values, f"index.json should name {want!r}: {index}")

    def test_forced_removal_of_an_unmerged_worker_keeps_its_records(self):  # covers: PRE-4
        self.spawn()
        self.worker_commit("src/pages.py", "pages = 1\n")
        self.write_records()
        r = self.fleet("remove", "m2", "--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.wt))
        self.assert_kept()

    def test_an_abandoned_worker_keeps_its_records(self):  # covers: PRE-4
        # stopped, never committed anything, then thrown away
        self.spawn()
        self.write_records()
        self.assertEqual(self.fleet("stop", "m2").returncode, 0)
        r = self.fleet("remove", "m2", "--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.wt))
        self.assert_kept()

    def test_a_worker_with_no_records_is_still_removed(self):  # covers: PRE-4
        # nothing to copy isn't a failed copy
        self.spawn()
        for rel in ("hooks.log", "gates/history.jsonl", "compactions.jsonl"):
            fp = os.path.join(self.wt, ".regista", *rel.split("/"))
            if os.path.exists(fp):
                os.remove(fp)
        r = self.fleet("remove", "m2", "--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.wt))

    def test_a_failed_copy_keeps_the_worktree_and_says_why(self):  # covers: PRE-4
        self.spawn()
        self.worker_commit("src/pages.py", "pages = 1\n")
        self.write_records()
        # a file where the milestone's log folder should go: the copy can't be made
        blocker = os.path.join(self.p.root, ".regista", "fleet", "logs", "M2")
        os.makedirs(os.path.dirname(blocker), exist_ok=True)
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("in the way\n")
        r = self.fleet("remove", "m2", "--force")
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertTrue(os.path.isdir(self.wt), "the worktree was removed although its records weren't copied")
        branch = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "refs/heads/regista/m2"],
                                cwd=self.p.root, capture_output=True, text=True)
        self.assertEqual(branch.returncode, 0, "the branch regista/m2 was deleted although its records weren't copied")
        for rel, data in RECORDS.items():
            with open(os.path.join(self.wt, ".regista", *rel.split("/")), "rb") as f:
                self.assertEqual(f.read(), data)
        self.assertIn("fleet/logs", out.replace("\\", "/"), "the command should say where it couldn't copy to")
        self.assertNotIn("Traceback", out)
        # once the obstacle is gone, the same command works
        os.remove(blocker)
        r = self.fleet("remove", "m2", "--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.wt))
        self.assert_kept()


if __name__ == "__main__":
    unittest.main()
