"""End to end: a worker's own records (hooks log, gate history, compactions, a review it never committed) are kept
in the main project when the worker is merged and its worktree removed through the real `regista.py fleet`
commands. Stand-ins for `claude` and `tmux` record their calls, so no real Claude session starts."""
import datetime
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from harness import PY, TOOL, SampleProject, bash  # noqa: E402

FAKE = r'''#!PYTHON
"""Stand-in for `claude` and `tmux`: records each call and keeps a session list."""
import json, os, sys, uuid
d = os.environ["FAKE_DIR"]
tool = os.path.basename(sys.argv[0])
args = sys.argv[1:]
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"tool": tool, "args": args, "cwd": os.getcwd()}) + "\n")
sp = os.path.join(d, "sessions.json")
sessions = json.load(open(sp)) if os.path.exists(sp) else []
def save():
    json.dump(sessions, open(sp, "w"))
if tool == "claude":
    if args[:2] == ["agents", "--json"]:
        print(json.dumps(sessions))
    elif "--bg" in args:
        name = args[args.index("-n") + 1]
        sessions.append({"name": name, "sessionId": str(uuid.uuid4()), "status": "idle", "id": "bg1234"})
        save()
        print("backgrounded · bg1234 · " + name)
    elif args[:1] == ["stop"]:
        sessions[:] = [s for s in sessions if s.get("id") != args[1]]
        save()
elif tool == "tmux":
    if args[0] == "new-session":
        name = args[args.index("-s") + 1]
        sessions.append({"name": name, "sessionId": str(uuid.uuid4()), "status": "idle"})
        save()
    elif args[0] == "capture-pane":
        print("/remote-control is active · Continue here, on your phone, or at https://claude.ai/code/session_TEST123")
    elif args[0] == "kill-session":
        name = args[args.index("-t") + 1]
        sessions[:] = [s for s in sessions if s.get("name") != name]
        save()
'''

PLAN = """# Plan

## M1 — Base
- [x] base step

## M2 — Pages
- [ ] the pages
"""


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


class WorkerLogsEndToEnd(unittest.TestCase):
    def setUp(self):
        self.sp = SampleProject()
        self.addCleanup(self.sp.cleanup)
        self.sp.set_up_regista()
        cfg = self.sp.config()
        cfg.update(review={"required_for": []},
                   requirements={"enforce_coverage": False, "milestone_needs_e2e": False})
        with open(self.sp.path(".regista/config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        self.sp.write(".regista/plan.md", PLAN)
        self.sp.git("add", "-A")
        self.sp.git("commit", "-q", "-m", "plan")
        self.fake = os.path.join(self.sp.base, "fake")
        os.makedirs(self.fake)
        for tool in ("claude", "tmux"):
            path = os.path.join(self.fake, tool)
            with open(path, "w", encoding="utf-8") as f:
                f.write(FAKE.replace("PYTHON", PY))
            os.chmod(path, 0o755)
        self.sp.env.update(REGISTA_CLAUDE=os.path.join(self.fake, "claude"),
                           REGISTA_TMUX=os.path.join(self.fake, "tmux"), FAKE_DIR=self.fake,
                           REGISTA_LAUNCH_WAIT="3", CLAUDE_CODE_SESSION_ID="overseer-1")
        self.addCleanup(self.sp.git, "worktree", "prune", check=False)
        self.wt = self.sp.path(".regista/worktrees/m2")

    def in_worker(self, *args, stdin=None):
        return subprocess.run(args, cwd=self.wt, input=stdin, capture_output=True, text=True, encoding="utf-8",
                              env=self.sp.env, timeout=600)

    def worker_write(self, rel, data):
        fp = os.path.join(self.wt, *rel.split("/"))
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "wb") as f:
            f.write(data)

    def test_a_merged_and_removed_worker_keeps_its_records(self):  # covers: PRE-1
        # the overseer's hook sees it run `regista.py fleet` and notes its session, then the real spawn
        self.sp.hook("pre-tool", {**bash(f'python3 "{TOOL}" fleet spawn M2'), "session_id": "overseer-1",
                                  "permission_mode": "acceptEdits"})
        r = self.sp.cli("fleet", "spawn", "M2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isdir(self.wt))

        # the worker's work, with a review it committed (the merge brings that one in)
        self.worker_write("pages.py", b"pages = 1\n")
        self.worker_write(".regista/reviews/committed1.json", b'{"verdict": "pass", "note": "committed"}\n')
        self.in_worker("git", "add", "-A")
        self.assertEqual(self.in_worker("git", "commit", "-q", "-m", "feat: pages").returncode, 0)

        # its own records, none of them committed: a refusal from its hooks, a real gate run, a compaction
        # and a review written after its last commit
        self.in_worker(PY, TOOL, "hook", "pre-tool",
                       stdin=json.dumps({**bash("git push origin regista/m2"), "cwd": self.wt}))
        r = self.in_worker(PY, TOOL, "gate")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.worker_write(".regista/compactions.jsonl",
                          '{"at": "2026-10-28T10:00:00", "trigger": "auto", "note": "ă"}\n'.encode("utf-8"))
        self.worker_write(".regista/reviews/late1.json", b'{"verdict": "pass", "note": "never committed"}')
        records = ["hooks.log", "gates/history.jsonl", "compactions.jsonl", "reviews/late1.json"]
        for rel in records:
            self.assertTrue(os.path.isfile(os.path.join(self.wt, ".regista", *rel.split("/"))),
                            f"setup: the worker should have written {rel}")

        r = self.sp.cli("fleet", "merge", "m2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        before = {rel: read_bytes(os.path.join(self.wt, ".regista", *rel.split("/"))) for rel in records}
        with open(self.sp.path(".regista/fleet/workers.json"), encoding="utf-8") as f:
            session_id = json.load(f)["m2"]["session_id"]
        self.assertTrue(session_id)

        r = self.sp.cli("fleet", "remove", "m2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.wt))

        kept = self.sp.path(".regista/fleet/logs/M2/m2")
        for rel, data in before.items():
            fp = os.path.join(kept, *rel.split("/"))
            self.assertTrue(os.path.isfile(fp), f"{rel} wasn't kept under .regista/fleet/logs/M2/m2/")
            self.assertEqual(read_bytes(fp), data, f"{rel} changed on the way")
        # the committed review reached the main reviews/ through the merge, so it isn't copied again
        self.assertTrue(os.path.isfile(self.sp.path(".regista/reviews/committed1.json")))
        self.assertFalse(os.path.exists(os.path.join(kept, "reviews", "committed1.json")))

        with open(os.path.join(kept, "index.json"), encoding="utf-8") as f:
            index = json.load(f)
        self.assertEqual(index.get("session_id"), session_id)
        values = [v for v in index.values() if isinstance(v, str)]
        for want in ("m2", "M2", "regista/m2"):
            self.assertIn(want, values, f"index.json should name {want!r}: {index}")
        self.assertTrue(any(v.replace("\\", "/").rstrip("/").endswith(".regista/worktrees/m2") for v in values),
                        f"index.json should name the worktree: {index}")

        def is_time(v):
            try:
                datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
                return True
            except ValueError:
                return False
        self.assertTrue(any(is_time(v) for v in values), f"index.json should say when it copied: {index}")


if __name__ == "__main__":
    unittest.main()
