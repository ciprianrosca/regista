"""End-to-end: a sample project set up through the real Regista CLI records what building costs. A fake builder
agent starts and stops through the real subagent-start and subagent-stop hooks, with its own fake transcript, then
a real commit goes through the real commit hook, all as subprocesses, against a local fake usage server whose
weekly percentage rises between calls (tests/fake_usage.py). .regista/usage.jsonl then holds the three records.

Every run gets a temporary home, state and cache folder and a temporary credentials file with a fake token.
Nothing here reaches the network, the real home folder or the real ~/.claude/.credentials.json.

Run: python3 -m unittest discover -s tests/e2e"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.append(os.path.dirname(HERE))  # for fake_usage; appended so tests/ never shadows tests/e2e/
from harness import CALC, SampleProject, bash, decision  # noqa: E402
from fake_usage import FAKE_TOKEN, FakeUsageServer, rising_replies, write_credentials  # noqa: E402

NOT_PLAN_VARS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")

SPEC = {"step": "sub", "milestone": "M1", "title": "Subtraction", "requirements": [
    {"id": "CALC-1", "text": "sub(a, b) SHALL return a minus b", "verify": "test"}]}

AGENT_ROWS = [
    {"type": "user", "message": {"role": "user", "content": "Add sub(a, b) to calc.py"}},
    {"type": "assistant", "message": {"id": "msg_1", "model": "claude-x", "usage": {
        "input_tokens": 3, "output_tokens": 50, "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 20000}}},
    {"type": "assistant", "message": {"id": "msg_1", "model": "claude-x", "usage": {
        "input_tokens": 3, "output_tokens": 50, "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 20000}}},
    {"type": "assistant", "message": {"id": "msg_2", "model": "claude-x", "usage": {
        "input_tokens": 4, "output_tokens": 70, "cache_creation_input_tokens": 500,
        "cache_read_input_tokens": 22000}}},
]
AGENT_TOKENS = {"input": 7, "output": 120, "cache_write": 1500, "cache_read": 42000}


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


class UsageRecordEndToEnd(unittest.TestCase):
    def setUp(self):
        self.p = SampleProject()
        self.addCleanup(self.p.cleanup)
        self.p.set_up_regista()
        cfg = self.p.config()
        cfg.update(review={"required_for": []},
                   requirements={"enforce_coverage": False, "milestone_needs_e2e": False})
        with open(self.p.path(".regista/config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        self.p.write(".regista/specs/sub.json", json.dumps(SPEC))
        self.p.git("add", "-A")
        self.p.git("commit", "-q", "-m", "spec: subtraction")

        self.srv = FakeUsageServer(replies=rising_replies(20, week_start=40.0, week_step=0.5)).start()
        self.addCleanup(self.srv.stop)
        creds = write_credentials(os.path.join(self.p.base, "credentials.json"))
        for k in NOT_PLAN_VARS:
            self.p.env.pop(k, None)
        self.p.env.update(REGISTA_USAGE_URL=self.srv.url, REGISTA_CREDENTIALS=creds, REGISTA_USAGE_CACHE_SECONDS="0",
                          XDG_STATE_HOME=os.path.join(self.p.base, "state"),
                          XDG_CACHE_HOME=os.path.join(self.p.base, "cache"), TZ="Europe/Bucharest")

    def records(self):
        path = self.p.path(".regista/usage.jsonl")
        self.assertTrue(os.path.isfile(path), "no .regista/usage.jsonl was written")
        with open(path, encoding="utf-8") as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def test_agent_and_step_records_with_rising_week(self):  # covers: UREC-10
        p = self.p
        folder = os.path.join(p.home, ".claude", "projects", "-sample-project")
        host = write_jsonl(os.path.join(folder, "sess-1.jsonl"),
                           [{"type": "user", "message": {"role": "user", "content": "build M1"}}])
        agent = write_jsonl(os.path.join(folder, "sess-1", "subagents", "agent-b1.jsonl"), AGENT_ROWS)
        base = {"session_id": "sess-1", "transcript_path": host, "agent_id": "b1", "agent_type": "regista:builder"}

        p.hook("subagent-start", {**base, "hook_event_name": "SubagentStart"})
        self.assertEqual(p.last_hook.returncode, 0, p.last_hook.stderr)

        # the builder's work
        p.write("calc.py", CALC + "\n\ndef sub(a, b):\n    return a - b\n")
        p.write("tests/test_sub.py", "import os\nimport sys\nimport unittest\n\n"
                "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
                "from calc import sub  # noqa: E402\n\n\n"
                "class SubTest(unittest.TestCase):\n"
                "    def test_subtracts(self):  # covers: CALC-1\n"
                "        self.assertEqual(sub(5, 3), 2)\n")

        p.hook("subagent-stop", {**base, "hook_event_name": "SubagentStop", "agent_transcript_path": agent,
                                 "stop_hook_active": False})
        self.assertEqual(p.last_hook.returncode, 0, p.last_hook.stderr)

        p.git("add", "-A")
        answer = p.hook("pre-tool", {**bash('git commit -m "feat: sub"'), "session_id": "sess-1"})
        self.assertNotEqual(decision(answer), "deny", f"commit refused: {answer}\n{p.last_hook.stderr}")
        p.git("commit", "-q", "-m", "feat: sub")

        recs = self.records()
        kinds = [r["kind"] for r in recs]
        for kind in ("agent_start", "agent_stop", "step"):
            self.assertIn(kind, kinds, recs)
        start = next(r for r in recs if r["kind"] == "agent_start")
        stop = next(r for r in recs if r["kind"] == "agent_stop")
        step = next(r for r in recs if r["kind"] == "step")
        self.assertLess(kinds.index("agent_start"), kinds.index("agent_stop"))
        self.assertLess(kinds.index("agent_stop"), kinds.index("step"))

        self.assertEqual((start["agent_id"], start["role"]), ("b1", "builder"))
        self.assertEqual((step["milestone"], step["step"]), ("M1", "sub"))
        self.assertEqual(step["requirements"], 1)
        self.assertEqual(step["commit_files"], 2)
        self.assertGreater(step["lines_changed"], 0)

        weeks = [r["usage"]["week_pct"] for r in recs]
        self.assertTrue(all(isinstance(w, (int, float)) for w in weeks), weeks)
        self.assertGreater(step["usage"]["week_pct"], recs[0]["usage"]["week_pct"], weeks)
        self.assertEqual(stop["tokens"], AGENT_TOKENS)
        self.assertEqual(stop["model"], "claude-x")

        with open(p.path(".regista/usage.jsonl"), encoding="utf-8") as f:
            self.assertNotIn(FAKE_TOKEN, f.read())


if __name__ == "__main__":
    unittest.main()
