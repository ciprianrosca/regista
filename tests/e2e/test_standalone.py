"""End-to-end: Regista works on its own, from a plain copy of its files and nothing else.

The plugin's tracked files (git ls-files) are copied into a temporary folder outside the repository. `claude plugin validate` must pass on the copy (skipped when claude isn't on PATH). Then, with the
copy's scripts/regista.py as the only Regista, a sample Python project goes through `init --auto`,
`gate --all`, a reviewer verdict recorded with `review-record`, and a commit through the copy's commit hook,
which runs the gate again. Every subprocess gets a temporary home folder (see harness.py).

Run: python3 -m unittest discover -s tests/e2e"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import CALC, PY, REPO, SampleProject, decision, reason  # noqa: E402

BIG = CALC + "".join(f"\n\ndef f{i}(x):\n    return x + {i}\n" for i in range(10))  # about 40 new lines


def copy_tracked_files(dest):
    """Copy the repository's tracked files into `dest`, keeping file modes."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    out = subprocess.run(["git", "-C", REPO, "ls-files", "-z"], capture_output=True, text=True, check=True,
                         env=env).stdout
    copied = 0
    for rel in filter(None, out.split("\0")):
        src = os.path.join(REPO, rel)
        if not os.path.isfile(src):  # tracked but deleted in this checkout
            continue
        dst = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    return copied


class CopiedProject(SampleProject):
    """The harness's sample project, driven by the copy's regista.py instead of the repository's."""

    def __init__(self, tool):
        self.tool = tool
        super().__init__()

    def cli(self, *args, timeout=600):
        return subprocess.run([PY, self.tool, *args], cwd=self.root, capture_output=True, text=True,
                              encoding="utf-8", env=self.env, timeout=timeout)

    def hook(self, event, payload=None, timeout=600):
        data = {**(payload or {}), "cwd": self.root}
        r = subprocess.run([PY, self.tool, "hook", event], input=json.dumps(data), cwd=self.root,
                           capture_output=True, text=True, encoding="utf-8", env=self.env, timeout=timeout)
        self.last_hook = r
        if r.returncode != 0 and not r.stdout.strip():
            raise AssertionError(f"hook {event} failed ({r.returncode}):\n{r.stderr}")
        return json.loads(r.stdout) if r.stdout.strip() else None


class RegistaOnItsOwn(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="regista-alone-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.copy = os.path.join(self.base, "regista")
        os.makedirs(self.copy)
        self.copied = copy_tracked_files(self.copy)
        self.tool = os.path.join(self.copy, "scripts", "regista.py")

    def assert_copy_is_alone(self):
        """The copy is real and outside the repository."""
        self.assertGreater(self.copied, 10)
        self.assertTrue(os.path.isfile(self.tool))
        self.assertTrue(os.path.isfile(os.path.join(self.copy, ".claude-plugin", "plugin.json")))
        real_copy, real_repo = os.path.realpath(self.copy), os.path.realpath(REPO)
        self.assertNotEqual(os.path.commonpath([real_copy, real_repo]), real_repo, "the copy is inside the repo")

    def test_claude_plugin_validate_passes_on_the_copy(self):
        self.assert_copy_is_alone()
        claude = shutil.which("claude")
        if not claude:
            self.skipTest("claude isn't on PATH, so `claude plugin validate` can't run here")
        home = os.path.join(self.base, "home")
        os.makedirs(home)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("REGISTA_", "DAEDALUS_", "GIT_")) and not k.endswith("_PROJECT_DIR")}
        env.update(HOME=home, XDG_CONFIG_HOME=os.path.join(home, ".config"))
        v = subprocess.run([claude, "plugin", "validate", self.copy], cwd=self.base, capture_output=True,
                           text=True, encoding="utf-8", env=env, timeout=120)
        self.assertEqual(v.returncode, 0, f"claude plugin validate failed on the copy:\n{v.stdout}\n{v.stderr}")

    def test_a_sample_project_runs_on_the_copy_alone(self):
        self.assert_copy_is_alone()
        p = CopiedProject(self.tool)
        self.addCleanup(p.cleanup)
        seen = []  # every output, checked at the end for a path into the repository

        def run(r):
            seen.append(r.stdout + r.stderr)
            return r

        # init --auto succeeds with the copy's regista.py.
        count = os.path.join(p.base, "gate-runs.log")
        r = run(p.cli("init", "--auto", "--name", "Sample"))
        self.assertEqual(r.returncode, 0, f"init --auto failed:\n{r.stdout}\n{r.stderr}")
        self.assertTrue(os.path.isfile(p.path(".regista/config.json")))
        cfg = p.config()
        cfg["gates"] = [{"name": "tests", "run": f'echo run >> "{count}" && "{PY}" -m unittest discover -s tests',
                         "when": ["commit"]}]
        cfg["review"] = {**cfg.get("review", {}), "required_for": ["calc.py"], "min_changed_lines": 15}
        with open(p.path(".regista/config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        p.git("add", "-A")
        p.git("commit", "-q", "-m", "set up Regista")

        def gate_runs():
            if not os.path.exists(count):
                return 0
            with open(count, encoding="utf-8") as f:
                return len(f.read().splitlines())

        # gate --all passes.
        g = run(p.cli("gate", "--all"))
        self.assertEqual(g.returncode, 0, f"gate --all failed:\n{g.stdout}\n{g.stderr}")
        self.assertEqual(gate_runs(), 1, "gate --all didn't run the project's gate")

        # A change big enough to need a review: the copy's hook refuses the commit until a verdict is recorded.
        p.write("calc.py", BIG)
        commit = p.bash('git add -A && git commit -m "add helpers"')
        out = p.hook("pre-tool", commit)
        seen.append(p.last_hook.stdout + p.last_hook.stderr)
        self.assertEqual(decision(out), "deny", f"unreviewed commit not refused: {out}\n{p.last_hook.stderr}")
        self.assertIn("no review", reason(out))
        p.write(".regista/reviews/draft.json", json.dumps(
            {"verdict": "approve", "summary": "adds helpers", "checked": ["spec", "edge cases"], "findings": []}))
        r = run(p.cli("review-record"))
        self.assertEqual(r.returncode, 0, f"review-record failed:\n{r.stdout}\n{r.stderr}")

        # With the review recorded, the copy's hook lets the commit through and runs the gate for it.
        out = p.hook("pre-tool", commit)
        seen.append(p.last_hook.stdout + p.last_hook.stderr)
        self.assertNotEqual(decision(out), "deny", f"reviewed commit refused: {out}\n{p.last_hook.stderr}")
        self.assertEqual(gate_runs(), 2, "the commit hook didn't run the gate for the new files")
        p.git("add", "-A")
        c = p.git("commit", "-q", "-m", "add helpers", check=False)
        self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
        self.assertIn("add helpers", p.git("log", "-1", "--format=%s").stdout)

        # No run reached back into the repository the copy came from.
        repo_dir = os.path.realpath(REPO) + os.sep
        self.assertEqual([o for o in seen if repo_dir in o or REPO + os.sep in o], [],
                         "a run named a path inside the repository")


if __name__ == "__main__":
    unittest.main()
