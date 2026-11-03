"""End-to-end: in a sample project with recorded steps (its .regista/usage.jsonl, shaped like the records the hooks
write), against a local fake usage server, `regista.py estimate M1` prints the expected cost as a range of the weekly
limit, the week's use and reset time, and whether the build fits, and exits 0.

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
from harness import SampleProject  # noqa: E402
from fake_usage import (SESSION_RESETS_AT, WEEK_RESETS_AT, WEEK_RESETS_TEXT_BUCHAREST, FakeUsageServer,  # noqa: E402
                        usage_reply, write_credentials)

NOT_PLAN_VARS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")

# M1 has two steps left. The four recorded steps used 2, 3, 2 and 3 million tokens and raised the week by 1, 1.5, 1
# and 1.5%: 2 million tokens per 1%, so a step costs 1 to 1.5% and M1 2 to 3% of the week.
PLAN = """# Sample: plan

## M1 — Arithmetic
Steps:
- [x] 1. add
- [ ] 2. sub
- [ ] 3. mul

## M2 — Docs
Steps:
- [ ] 1. a README
"""


def at(minute):
    return f"2026-11-03T{10 + minute // 60:02d}:{minute % 60:02d}:00+02:00"


def reading(week):
    return {"state": "ok", "session_pct": 10.0, "week_pct": week, "session_resets_at": SESSION_RESETS_AT,
            "week_resets_at": WEEK_RESETS_AT}


def recorded_steps():
    rows, t, week = [], 0, 30.0
    for i, (tokens, rise) in enumerate([(2_000_000, 1.0), (3_000_000, 1.5), (2_000_000, 1.0), (3_000_000, 1.5)]):
        name = f"past-{i + 1}"
        common = {"milestone": "M0", "step": name, "session_id": "s1", "other_sessions": 0}
        rows.append({**common, "kind": "agent_start", "at": at(t), "usage": reading(week), "agent_id": f"a{i}",
                     "role": "builder", "agent_type": "regista:builder"})
        rows.append({**common, "kind": "agent_stop", "at": at(t + 5), "usage": reading(week), "agent_id": f"a{i}",
                     "role": "builder", "agent_type": "regista:builder", "model": "claude-x", "seconds": 300.0,
                     "tokens": {"input": 10, "output": 1000, "cache_write": 9000, "cache_read": tokens - 10010}})
        week += rise
        rows.append({**common, "kind": "step", "at": at(t + 20), "usage": reading(week), "commit_files": 3,
                     "lines_changed": 40, "requirements": 2})
        t += 30
    return rows


class EstimateEndToEnd(unittest.TestCase):
    def setUp(self):
        self.p = SampleProject()
        self.addCleanup(self.p.cleanup)
        self.p.set_up_regista()
        self.p.write(".regista/plan.md", PLAN)
        self.p.git("add", "-A")
        self.p.git("commit", "-q", "-m", "plan")
        with open(self.p.path(".regista/usage.jsonl"), "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r) + "\n" for r in recorded_steps())

        self.srv = FakeUsageServer(reply=usage_reply(37.0, 60.0)).start()
        self.addCleanup(self.srv.stop)
        creds = write_credentials(os.path.join(self.p.base, "credentials.json"))
        for k in NOT_PLAN_VARS:
            self.p.env.pop(k, None)
        self.p.env.update(REGISTA_USAGE_URL=self.srv.url, REGISTA_CREDENTIALS=creds, REGISTA_USAGE_CACHE_SECONDS="0",
                          XDG_STATE_HOME=os.path.join(self.p.base, "state"),
                          XDG_CACHE_HOME=os.path.join(self.p.base, "cache"), TZ="Europe/Bucharest")

    def test_estimate_before_building_m1(self):  # covers: UEST-8
        r = self.p.cli("estimate", "M1")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertNotIn("Traceback", out)
        head = next(ln for ln in r.stdout.splitlines() if ln.strip())
        m = re.search(r"(?<![\d.])(\d+(?:\.\d+)?) to (\d+(?:\.\d+)?)\s*% of your week", head)
        self.assertTrue(m, f"the first line should give the cost as a range of the week:\n{r.stdout}")
        self.assertEqual((float(m.group(1)), float(m.group(2))), (2.0, 3.0))
        self.assertRegex(r.stdout, r"(?<![\d.])60(\.0)?\s*%", "the week's use")
        self.assertIn(WEEK_RESETS_TEXT_BUCHAREST, r.stdout)
        self.assertIn("fit", r.stdout.lower())
        self.assertGreater(self.srv.count, 0, "the week's use should come from the usage server")


if __name__ == "__main__":
    unittest.main()
