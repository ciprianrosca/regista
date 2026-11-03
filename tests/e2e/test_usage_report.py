"""End-to-end: after a step is built in a sample project through the real Regista CLI and hooks (a fake builder
starts and stops through the real subagent-start and subagent-stop hooks, then a real commit goes through the real
commit hook), against a local fake usage server whose weekly percentage rises between calls, `regista.py usage
report` shows that step's cost: the rise in the weekly percentage and the builder's tokens.

Every run gets a temporary home, state and cache folder and a temporary credentials file with a fake token.
Nothing here reaches the network, the real home folder or the real ~/.claude/.credentials.json.

Run: python3 -m unittest discover -s tests/e2e"""
import json
import os
import re
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

# Two model calls (msg_1 repeated, as Claude Code does): 3,241,627 tokens in all, "3.2 million tokens".
AGENT_ROWS = [
    {"type": "user", "message": {"role": "user", "content": "Add sub(a, b) to calc.py"}},
    {"type": "assistant", "message": {"id": "msg_1", "model": "claude-x", "usage": {
        "input_tokens": 3, "output_tokens": 50, "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 2000000}}},
    {"type": "assistant", "message": {"id": "msg_1", "model": "claude-x", "usage": {
        "input_tokens": 3, "output_tokens": 50, "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 2000000}}},
    {"type": "assistant", "message": {"id": "msg_2", "model": "claude-x", "usage": {
        "input_tokens": 4, "output_tokens": 70, "cache_creation_input_tokens": 40500,
        "cache_read_input_tokens": 1200000}}},
]
AGENT_TOTAL = 3 + 50 + 1000 + 2000000 + 4 + 70 + 40500 + 1200000


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


class UsageReportEndToEnd(unittest.TestCase):
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

    def build_the_step(self):
        p = self.p
        folder = os.path.join(p.home, ".claude", "projects", "-sample-project")
        host = write_jsonl(os.path.join(folder, "sess-1.jsonl"),
                           [{"type": "user", "message": {"role": "user", "content": "build M1"}}])
        agent = write_jsonl(os.path.join(folder, "sess-1", "subagents", "agent-b1.jsonl"), AGENT_ROWS)
        base = {"session_id": "sess-1", "transcript_path": host, "agent_id": "b1", "agent_type": "regista:builder"}

        p.hook("subagent-start", {**base, "hook_event_name": "SubagentStart"})
        self.assertEqual(p.last_hook.returncode, 0, p.last_hook.stderr)
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

    def recorded_rise(self):
        with open(self.p.path(".regista/usage.jsonl"), encoding="utf-8") as f:
            recs = [json.loads(ln) for ln in f if ln.strip()]
        first = next(r for r in recs if r["kind"] == "agent_start")
        step = [r for r in recs if r["kind"] == "step"][-1]
        return step["usage"]["week_pct"] - first["usage"]["week_pct"]

    def test_the_built_step_shows_its_cost(self):  # covers: UREP-8
        self.build_the_step()
        rise = self.recorded_rise()
        self.assertGreater(rise, 0, "setup: the fake server's week percentage should have risen")

        r = self.p.cli("usage", "report")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("sub", r.stdout)
        forms = {f"{rise:.1f}", f"{rise:g}"}
        self.assertTrue(any(re.search(re.escape(f) + r"\s*%", r.stdout) for f in forms),
                        f"the report should show the {rise:g}% rise:\n{r.stdout}")
        self.assertIn("3.2 million tokens", r.stdout)
        self.assertIn("builder", r.stdout)
        self.assertNotIn(FAKE_TOKEN, out)

        r = self.p.cli("usage", "report", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        data = json.loads(r.stdout)
        step = next(s for s in data["steps"] if (s["milestone"], s["step"]) == ("M1", "sub"))
        self.assertAlmostEqual(step["week_pct"], rise)
        self.assertEqual(step["tokens_total"], AGENT_TOTAL)
        self.assertEqual(step["tokens_by_role"].get("builder"), AGENT_TOTAL)
        self.assertEqual(step["agents"], 1)


if __name__ == "__main__":
    unittest.main()
