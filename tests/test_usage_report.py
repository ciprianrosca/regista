"""Tests for the usage report: usage_report(root, cfg, milestone=None), format_usage_report(report) and
`regista.py usage report [M<n>] [--json]`, fed with hand-written .regista/usage.jsonl lines shaped like the ones the
hooks write (tests/test_usage_record.py shows how the real hooks write them). Also: the milestone's cost in
`regista.py status`, the save skill's instructions, and usage.jsonl among the harness's ignored local state and a
fleet worker's kept records. The end-to-end check is in tests/e2e/test_usage_report.py.

Nothing here reaches the network or the real home folder: HOME, XDG_STATE_HOME and XDG_CACHE_HOME are temporary,
the usage address points at a closed port on 127.0.0.1 and the credentials file doesn't exist.

Run: python3 -m unittest discover -s tests"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_regista import FAKE, FLEET_PLAN, PY, TOOL, Project, bash, jload, sh  # noqa: E402
from fake_usage import SESSION_RESETS_AT, WEEK_RESETS_AT, closed_port_url  # noqa: E402
import regista  # noqa: E402  (test_regista put scripts/ on the path)

STATE = tempfile.mkdtemp(prefix="regista-urep-state-")
os.environ.update(XDG_STATE_HOME=os.path.join(STATE, "state"), XDG_CACHE_HOME=os.path.join(STATE, "cache"),
                  REGISTA_USAGE_URL=closed_port_url(), REGISTA_CREDENTIALS=os.path.join(STATE, "no-credentials.json"))
for _k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "REGISTA_USAGE_CACHE_SECONDS"):
    os.environ.pop(_k, None)

NEXT_RESET = "2026-11-13T05:00:00+00:00"  # a week after WEEK_RESETS_AT: the week reset in between


# --- hand-written records, shaped like the ones the hooks write

def at(minute):
    """A local ISO time with offset, `minute` minutes after 10:00 on 3 November 2026."""
    return f"2026-11-03T{10 + minute // 60:02d}:{minute % 60:02d}:00+02:00"


def reading(week, session=10.0, week_resets=WEEK_RESETS_AT):
    known = week is not None or session is not None
    return {"state": "ok" if known else "unknown", "session_pct": session, "week_pct": week,
            "session_resets_at": SESSION_RESETS_AT, "week_resets_at": week_resets}


def tokens_of(total):
    """A tokens object whose four counts add up to `total` (None stays None)."""
    if total is None:
        return None
    return {"input": 1, "output": 2, "cache_write": 3, "cache_read": total - 6}


def agent(kind, ms, step, minute, week, session=10.0, role="builder", agent_id="a1", other=0,
          week_resets=WEEK_RESETS_AT, tokens=0):
    r = {"kind": kind, "at": at(minute), "milestone": ms, "step": step, "session_id": "s1",
         "usage": reading(week, session, week_resets), "other_sessions": other, "agent_id": agent_id, "role": role,
         "agent_type": f"regista:{role}"}
    if kind == "agent_stop":
        r.update(tokens=tokens if isinstance(tokens, dict) else tokens_of(tokens), model="claude-x", seconds=60.0)
    return r


def start(ms, step, minute, week, **kw):
    return agent("agent_start", ms, step, minute, week, **kw)


def stop(ms, step, minute, week, tokens=0, **kw):
    return agent("agent_stop", ms, step, minute, week, tokens=tokens, **kw)


def done(ms, step, minute, week, session=12.0, commit_files=2, lines=20, requirements=3, other=0,
         week_resets=WEEK_RESETS_AT):
    return {"kind": "step", "at": at(minute), "milestone": ms, "step": step, "session_id": "s1",
            "usage": reading(week, session, week_resets), "other_sessions": other, "commit_files": commit_files,
            "lines_changed": lines, "requirements": requirements}


def built(ms, step, minute, week_from, week_to, tokens, role="builder", other=0):
    """One step built by one agent: its start, its stop with `tokens`, and the step's commit."""
    return [start(ms, step, minute, week_from, other=other, role=role),
            stop(ms, step, minute + 5, week_from, tokens=tokens, other=other, role=role),
            done(ms, step, minute + 10, week_to, other=other)]


class ReportBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = Project()
        cls.root = cls.p.root
        cls.cfg = regista.load_config(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.p.cleanup()

    def setUp(self):
        if os.path.exists(self.usage_file()):
            os.remove(self.usage_file())

    def usage_file(self):
        return os.path.join(self.root, ".regista", "usage.jsonl")

    def write_records(self, rows):
        with open(self.usage_file(), "w", encoding="utf-8") as f:
            for r in rows:
                f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")

    def report(self, rows=None, milestone=None):
        if rows is not None:
            self.write_records(rows)
        return regista.usage_report(self.root, self.cfg, milestone=milestone)

    def step(self, rep, ms, step):
        found = [s for s in rep["steps"] if s["milestone"] == ms and s["step"] == step]
        self.assertEqual(len(found), 1, f"expected one entry for {ms} {step}: {rep['steps']}")
        return found[0]

    def milestone(self, rep, ms):
        found = [m for m in rep["milestones"] if m["milestone"] == ms]
        self.assertEqual(len(found), 1, f"expected one entry for {ms}: {rep['milestones']}")
        return found[0]

    def cli(self, *args):
        r = self.p.cli("usage", "report", *args)
        self.assertNotIn("Traceback", r.stdout + r.stderr, r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout


class StepRecords(ReportBase):
    def test_a_step_is_its_agent_records_and_its_last_step_record(self):  # covers: UREP-1
        rep = self.report([
            start("M1", "alpha", 0, 40.0, agent_id="a1"),
            stop("M1", "alpha", 5, 40.5, tokens=1000, agent_id="a1"),
            # another step's agents, interleaved: not alpha's
            start("M1", "beta", 6, 40.5, agent_id="b1"),
            stop("M1", "beta", 8, 40.7, tokens=500, agent_id="b1"),
            done("M1", "alpha", 10, 41.0, commit_files=3, lines=10, requirements=2),
            done("M1", "alpha", 12, 41.5, commit_files=5, lines=40, requirements=4),  # the last one counts
            done("M1", "alpha", 13, 41.6, commit_files=0, lines=0),  # a save commit: ignored
            done("M1", "gamma", 14, 41.7, commit_files=0, lines=0),  # a status-only commit: no step
        ])
        alpha = self.step(rep, "M1", "alpha")
        self.assertEqual((alpha["commit_files"], alpha["lines_changed"], alpha["requirements"]), (5, 40, 4))
        self.assertEqual(alpha["tokens_total"], 1000)
        self.assertEqual(alpha["agents"], 1)
        self.assertAlmostEqual(alpha["week_pct"], 1.5)  # 41.5, not 41.0 or 41.6, minus 40.0
        self.assertEqual([s for s in rep["steps"] if s["step"] == "gamma"], [])
        self.assertIsInstance(rep["records"], int)

    def test_records_are_taken_in_time_order_not_file_order(self):  # covers: UREP-1
        # The project's own lines first, then a fleet worker's lines appended at the end, although the worker's
        # step happened earlier. Taken in file order, alpha would start from w2's 41.0 at most and w1 or w2
        # would be measured from alpha's later readings (negative percentages and seconds).
        project = [
            stop("M1", "alpha", 35, 44.5, tokens=100),  # alpha has no agent_start: it starts at the previous step
            done("M1", "alpha", 40, 45.0),
        ]
        worker = [
            *built("M2", "w1", 0, 40.0, 40.5, 200),
            stop("M2", "w2", 12, 40.5, tokens=300, agent_id="w2"),  # no agent_start either
            done("M2", "w2", 20, 41.0),
        ]
        rep = self.report(project + worker)
        w1, w2, alpha = (self.step(rep, ms, s) for ms, s in (("M2", "w1"), ("M2", "w2"), ("M1", "alpha")))
        self.assertAlmostEqual(w1["week_pct"], 0.5)
        self.assertAlmostEqual(w1["seconds"], 600.0)
        self.assertAlmostEqual(w2["week_pct"], 0.5)  # from w1's commit at 40.5, the previous one in time
        self.assertAlmostEqual(alpha["week_pct"], 4.0)  # from w2's commit at 41.0, the previous one in time
        for s in rep["steps"]:
            for key in ("week_pct", "session_pct", "seconds"):
                self.assertTrue(s[key] is None or s[key] >= 0, f"{s['step']} {key} is negative: {s[key]}")
        self.assertAlmostEqual(self.milestone(rep, "M2")["week_pct"], 1.0)

    def test_shuffled_lines_give_the_same_report(self):  # covers: UREP-1
        rows = [*built("M1", "alpha", 0, 40.0, 41.0, 100), *built("M1", "beta", 20, 41.0, 42.5, 200)]
        ordered = self.report(rows)
        shuffled = self.report([rows[4], rows[2], rows[5], rows[0], rows[3], rows[1]])
        self.assertEqual(ordered["steps"], shuffled["steps"])
        self.assertEqual(ordered["milestones"], shuffled["milestones"])
        self.assertAlmostEqual(self.step(shuffled, "M1", "beta")["week_pct"], 1.5)

    def test_times_with_different_offsets_are_compared_as_times(self):  # covers: UREP-1
        # 08:05 UTC is 10:05 at +02:00: the stop comes between the start (10:00) and the commit (10:10).
        utc_stop = {**stop("M1", "alpha", 5, 40.5, tokens=100), "at": "2026-11-03T08:05:00+00:00"}
        rep = self.report([done("M1", "alpha", 10, 41.0), utc_stop, start("M1", "alpha", 0, 40.0)])
        alpha = self.step(rep, "M1", "alpha")
        self.assertAlmostEqual(alpha["week_pct"], 1.0)
        self.assertAlmostEqual(alpha["seconds"], 600.0)
        self.assertEqual(alpha["tokens_total"], 100)

    def test_records_without_a_readable_time_come_after_the_timed_ones(self):  # covers: UREP-1
        untimed = {k: v for k, v in stop("M1", "alpha", 5, 40.5, tokens=700).items() if k != "at"}
        rep = self.report([untimed, *built("M1", "alpha", 0, 40.0, 41.0, 100)])
        alpha = self.step(rep, "M1", "alpha")
        self.assertEqual(alpha["tokens_total"], 800)
        self.assertAlmostEqual(alpha["week_pct"], 1.0)  # still from the agent_start, the earliest timed record
        self.assertAlmostEqual(alpha["seconds"], 600.0)

    def test_a_step_record_with_an_empty_step_name_is_ignored(self):  # covers: UREP-1
        rep = self.report([
            *built("M1", "alpha", 0, 40.0, 41.0, 100),
            done("M1", "", 15, 42.0, commit_files=4),  # a commit made before any spec existed
            stop("M1", "beta", 18, 42.2, tokens=10),
            done("M1", "beta", 20, 42.5),
        ])
        self.assertEqual([s["step"] for s in rep["steps"] if not s["step"]], [])
        self.assertAlmostEqual(self.step(rep, "M1", "beta")["week_pct"], 1.5)  # from alpha's 41.0, not 42.0
        self.assertEqual(self.milestone(rep, "M1")["steps"], 2)


class Percentages(ReportBase):
    def test_week_and_session_rise_from_the_first_agent_start(self):  # covers: UREP-2
        rep = self.report([
            start("M1", "alpha", 0, 40.0, session=10.0),
            start("M1", "alpha", 2, 40.25, session=11.0, agent_id="a2", role="test-writer"),
            stop("M1", "alpha", 5, 41.0, session=13.0, tokens=10),
            done("M1", "alpha", 10, 42.5, session=16.0),
        ])
        alpha = self.step(rep, "M1", "alpha")
        self.assertAlmostEqual(alpha["week_pct"], 2.5)
        self.assertAlmostEqual(alpha["session_pct"], 6.0)
        self.assertFalse(alpha["week_reset"])

    def test_without_an_agent_start_the_previous_counted_step_is_the_start(self):  # covers: UREP-2
        rep = self.report([
            *built("M1", "alpha", 0, 40.0, 41.0, 100),
            done("M1", "alpha", 11, 41.2, commit_files=0),  # not counted: not a start for beta
            stop("M1", "beta", 15, 41.5, tokens=10),  # an agent_stop isn't a start either
            done("M1", "beta", 20, 41.75),
        ])
        self.assertAlmostEqual(self.step(rep, "M1", "beta")["week_pct"], 0.75)

    def test_a_first_step_with_no_earlier_reading_has_no_percentage(self):  # covers: UREP-2
        rep = self.report([done("M1", "alpha", 10, 41.0)])
        alpha = self.step(rep, "M1", "alpha")
        self.assertIsNone(alpha["week_pct"])
        self.assertIsNone(alpha["session_pct"])

    def test_an_unknown_reading_gives_null(self):  # covers: UREP-2
        rep = self.report([
            start("M1", "alpha", 0, None, session=10.0),  # week unknown at the start
            done("M1", "alpha", 10, 41.0, session=14.0),
            start("M1", "beta", 20, 41.0, session=14.0),
            done("M1", "beta", 30, 42.0, session=None),  # session unknown at the end
            start("M1", "gamma", 40, 42.0, session=None, week_resets=WEEK_RESETS_AT),
            {**done("M1", "gamma", 50, None, session=None), "usage": {"state": "unknown", "session_pct": None,
                                                                      "week_pct": None, "session_resets_at": None,
                                                                      "week_resets_at": None}},
        ])
        alpha, beta, gamma = (self.step(rep, "M1", s) for s in ("alpha", "beta", "gamma"))
        self.assertIsNone(alpha["week_pct"])
        self.assertAlmostEqual(alpha["session_pct"], 4.0)
        self.assertAlmostEqual(beta["week_pct"], 1.0)
        self.assertIsNone(beta["session_pct"])
        self.assertIsNone(gamma["week_pct"])
        self.assertIsNone(gamma["session_pct"])

    def test_a_week_reset_in_between_gives_null_and_says_so(self):  # covers: UREP-2
        rep = self.report([
            start("M1", "alpha", 0, 95.0, session=10.0, week_resets=WEEK_RESETS_AT),
            done("M1", "alpha", 10, 2.0, session=15.0, week_resets=NEXT_RESET),
        ])
        alpha = self.step(rep, "M1", "alpha")
        self.assertIsNone(alpha["week_pct"])
        self.assertTrue(alpha["week_reset"])
        self.assertAlmostEqual(alpha["session_pct"], 5.0)

    def test_a_lower_reading_at_the_end_is_a_reset_even_without_both_reset_times(self):  # covers: UREP-2
        rep = self.report([
            start("M1", "alpha", 0, 95.0, session=10.0, week_resets=WEEK_RESETS_AT),
            done("M1", "alpha", 10, 2.0, session=15.0, week_resets=None),  # reset time missing at the end
            start("M1", "beta", 20, 96.0, session=20.0, week_resets=None),  # missing at the start
            done("M1", "beta", 30, 3.0, session=25.0, week_resets=NEXT_RESET),
            start("M1", "gamma", 40, 41.0, session=30.0),
            done("M1", "gamma", 50, 40.5, session=31.0),  # same reset time, a little lower at the end
        ])
        for name in ("alpha", "beta", "gamma"):
            s = self.step(rep, "M1", name)
            self.assertIsNone(s["week_pct"], name)
            self.assertTrue(s["week_reset"], name)
        self.assertAlmostEqual(self.step(rep, "M1", "alpha")["session_pct"], 5.0)
        text = regista.format_usage_report(rep)
        self.assertNotRegex(text, r"-\s*\d+(\.\d+)?\s*%", text)

    def test_a_lower_session_reading_at_the_end_gives_null(self):  # covers: UREP-2
        rep = self.report([
            start("M1", "alpha", 0, 40.0, session=80.0),
            done("M1", "alpha", 10, 41.0, session=5.0),
        ])
        alpha = self.step(rep, "M1", "alpha")
        self.assertIsNone(alpha["session_pct"])
        self.assertAlmostEqual(alpha["week_pct"], 1.0)
        self.assertFalse(alpha["week_reset"])
        self.assertNotRegex(regista.format_usage_report(rep), r"-\s*\d+(\.\d+)?\s*%")


class TokensAgentsTime(ReportBase):
    def test_tokens_by_role_agents_and_seconds(self):  # covers: UREP-3
        rep = self.report([
            start("M1", "alpha", 0, 40.0, role="test-writer", agent_id="t1"),
            stop("M1", "alpha", 4, 40.1, role="test-writer", agent_id="t1", tokens=None),  # unreadable: counts 0
            start("M1", "alpha", 5, 40.1, agent_id="b1"),
            stop("M1", "alpha", 15, 40.5, agent_id="b1",
                 tokens={"input": 10, "output": 2000, "cache_write": 30000, "cache_read": 1000000}),
            start("M1", "alpha", 16, 40.5, agent_id="b2"),
            stop("M1", "alpha", 20, 40.7, agent_id="b2", tokens=500000),
            start("M1", "alpha", 21, 40.7, role="reviewer", agent_id="r1"),
            stop("M1", "alpha", 25, 40.8, role="reviewer", agent_id="r1", tokens=250000),
            done("M1", "alpha", 30, 41.0),
        ])
        alpha = self.step(rep, "M1", "alpha")
        self.assertEqual(alpha["tokens_by_role"]["builder"], 1032010 + 500000)
        self.assertEqual(alpha["tokens_by_role"]["reviewer"], 250000)
        self.assertEqual(alpha["tokens_by_role"].get("test-writer", 0), 0)
        self.assertEqual(alpha["tokens_total"], 1032010 + 500000 + 250000)
        self.assertEqual(alpha["agents"], 4)
        self.assertAlmostEqual(alpha["seconds"], 1800.0)


class OtherSessions(ReportBase):
    def test_other_sessions_on_any_record_marks_the_step_and_milestone(self):  # covers: UREP-4
        rep = self.report([
            *built("M1", "alpha", 0, 40.0, 41.0, 100),
            start("M1", "beta", 20, 41.0),
            stop("M1", "beta", 25, 41.5, tokens=100, other=2),  # one record only
            done("M1", "beta", 30, 42.0),
            *built("M2", "gamma", 40, 42.0, 43.0, 100),
        ])
        self.assertFalse(self.step(rep, "M1", "alpha")["other_sessions"])
        self.assertTrue(self.step(rep, "M1", "beta")["other_sessions"])
        self.assertTrue(self.milestone(rep, "M1")["other_sessions"])
        self.assertFalse(self.milestone(rep, "M2")["other_sessions"])


MILESTONE_ROWS = [
    start("M1", "alpha-step", 0, 40.0),
    stop("M1", "alpha-step", 5, 40.5, tokens=1000000),
    done("M1", "alpha-step", 10, 41.0),
    start("M1", "beta-step", 12, 41.2, role="reviewer"),
    stop("M1", "beta-step", 15, 41.5, tokens=2000000, role="reviewer"),
    stop("M1", "beta-step", 16, 41.6, tokens=500000, agent_id="a9"),
    done("M1", "beta-step", 20, 43.0),
    *built("M2", "gamma-step", 30, 43.0, 44.0, 700000),
    start("M3", "delta-step", 50, 96.0, week_resets=WEEK_RESETS_AT),
    done("M3", "delta-step", 55, 97.0, week_resets=WEEK_RESETS_AT),
    start("M3", "epsilon-step", 60, 1.0, week_resets=NEXT_RESET),
    done("M3", "epsilon-step", 65, 2.0, week_resets=NEXT_RESET),
]


class Milestones(ReportBase):
    def test_a_milestone_sums_its_steps(self):  # covers: UREP-5
        rep = self.report(MILESTONE_ROWS)
        m1 = self.milestone(rep, "M1")
        self.assertEqual(m1["steps"], 2)
        self.assertAlmostEqual(m1["week_pct"], 3.0)  # 43.0 at beta's commit minus 40.0 at alpha's start
        self.assertEqual(m1["tokens_total"], 3500000)
        self.assertEqual(m1["tokens_by_role"], {"builder": 1500000, "reviewer": 2000000})
        self.assertAlmostEqual(self.milestone(rep, "M2")["week_pct"], 1.0)
        m3 = self.milestone(rep, "M3")
        self.assertIsNone(m3["week_pct"])  # the week reset between its first and last reading
        self.assertEqual(m3["steps"], 2)

    def test_a_milestone_with_an_unknown_last_reading_has_no_percentage(self):  # covers: UREP-5
        rep = self.report([*built("M1", "alpha", 0, 40.0, 41.0, 100),
                           start("M1", "beta", 20, 41.0), done("M1", "beta", 30, None, session=None)])
        self.assertIsNone(self.milestone(rep, "M1")["week_pct"])

    def test_a_milestone_starts_at_an_agent_record_of_a_step_counted_later(self):  # covers: UREP-5
        rep = self.report([
            start("M1", "beta", 0, 40.0, agent_id="b1"),  # beta starts first but is committed last
            *built("M1", "alpha", 5, 40.5, 41.0, 100),
            done("M1", "beta", 30, 42.0),
        ])
        self.assertAlmostEqual(self.milestone(rep, "M1")["week_pct"], 2.0)  # 42.0 minus beta's 40.0, not 40.5

    def test_a_milestone_starts_at_its_earliest_record_of_any_kind(self):  # covers: UREP-5
        rep = self.report([
            start("M1", "spike", 0, 39.0, agent_id="s1"),  # a step that was never committed
            stop("M1", "spike", 3, 39.5, agent_id="s1", tokens=50),
            *built("M1", "alpha", 10, 40.0, 41.0, 100),
        ])
        m1 = self.milestone(rep, "M1")
        self.assertAlmostEqual(m1["week_pct"], 2.0)  # 41.0 minus 39.0, not alpha's 40.0
        self.assertEqual(m1["steps"], 1)

    def test_one_milestone_only(self):  # covers: UREP-5
        rep = self.report(MILESTONE_ROWS, milestone="M2")
        self.assertEqual([(s["milestone"], s["step"]) for s in rep["steps"]], [("M2", "gamma-step")])
        self.assertEqual([m["milestone"] for m in rep["milestones"]], ["M2"])
        data = json.loads(self.cli("M2", "--json"))
        self.assertEqual([(s["milestone"], s["step"]) for s in data["steps"]], [("M2", "gamma-step")])
        self.assertEqual([m["milestone"] for m in data["milestones"]], ["M2"])
        text = self.cli("M2")
        self.assertIn("gamma-step", text)
        for other in ("alpha-step", "beta-step", "delta-step", "epsilon-step"):
            self.assertNotIn(other, text)


class Estimate(ReportBase):
    def test_tokens_per_pct_from_clean_steps_only(self):  # covers: UREP-6
        rep = self.report([
            *built("M1", "a", 0, 40.0, 42.0, 200000000),  # counts
            *built("M1", "b", 20, 42.0, 43.0, 50000000),  # counts
            *built("M1", "c", 40, 43.0, 44.0, 10000000, other=1),  # other sessions: left out
            start("M1", "d", 60, None), stop("M1", "d", 65, None, tokens=7000000), done("M1", "d", 70, 45.0),
            *built("M1", "e", 80, 45.0, 45.0, 5000000),  # 0%: left out
            done("M1", "a", 95, 45.5, commit_files=0),  # a save commit: changes nothing
        ])
        self.assertAlmostEqual(rep["tokens_per_pct"], 250000000 / 3.0, delta=1)
        self.assertEqual(rep["tokens_per_pct_based_on"], 2)

    def test_no_clean_step_no_estimate(self):  # covers: UREP-6
        rep = self.report([*built("M1", "c", 0, 43.0, 44.0, 10000000, other=1)])
        self.assertIsNone(rep["tokens_per_pct"])
        self.assertEqual(rep["tokens_per_pct_based_on"], 0)


def pct(text, value):
    """Whether `value` percent appears in text, written with one decimal or as few as needed (2.5%, 2.5 %)."""
    forms = {f"{value:.1f}", f"{value:g}"}
    return any(re.search(re.escape(f) + r"\s*%", text) for f in forms)


class TextReport(ReportBase):
    def test_plain_words_for_steps_milestones_and_estimate(self):  # covers: UREP-7
        rep = self.report([
            start("M1", "alpha", 0, 40.0),
            stop("M1", "alpha", 5, 41.0, tokens=2000000),
            start("M1", "alpha", 6, 41.0, role="reviewer", agent_id="r1"),
            stop("M1", "alpha", 8, 42.0, tokens=1200000, role="reviewer", agent_id="r1"),
            done("M1", "alpha", 10, 42.5),
            *built("M1", "beta", 20, 42.5, 43.7, 500000, other=3),
        ])
        text = regista.format_usage_report(rep)
        self.assertIsInstance(text, str)
        self.assertIn("3.2 million tokens", text)  # alpha
        self.assertIn("3.7 million tokens", text)  # M1
        self.assertTrue(pct(text, 2.5), text)  # alpha's share of the week
        self.assertTrue(pct(text, 1.2), text)  # beta's
        self.assertTrue(pct(text, 3.7), text)  # M1's
        self.assertIn("builder", text)
        self.assertIn("reviewer", text)
        self.assertIn("1.3 million", text)  # the estimate: 3.2 million over 2.5%, beta left out
        self.assertRegex(text, r"\b1 step\b")  # what it rests on
        self.assertIn("other sessions", text.lower())
        self.assertIn("alpha", text)
        self.assertIn("beta", text)
        self.assertNotRegex(text, r"[{}]|None|null")  # plain words, not a dump

    def test_does_not_claim_whole_percentages(self):  # covers: UREP-7
        rep = self.report([*built("M1", "alpha", 0, 40.0, 40.0, 300000), *built("M1", "beta", 20, 40.0, 42.0, 5000)])
        text = regista.format_usage_report(rep).lower()
        self.assertNotRegex(text, r"whole[ -](number|percent)|integer|rounds? (to|down|up)|in steps of 1\s*%", text)

    def test_no_other_sessions_no_note(self):  # covers: UREP-7
        text = regista.format_usage_report(self.report(built("M1", "alpha", 0, 40.0, 42.5, 3200000)))
        self.assertNotIn("other sessions", text.lower())

    def test_no_records_says_so(self):  # covers: UREP-7
        rep = self.report()
        self.assertEqual((rep["steps"], rep["milestones"], rep["records"]), ([], [], 0))
        self.assertIsNone(rep["tokens_per_pct"])
        self.assertRegex(regista.format_usage_report(rep).lower(), r"no usage has been recorded yet")
        self.assertRegex(self.cli().lower(), r"no usage has been recorded yet")
        self.assertEqual(json.loads(self.cli("--json"))["records"], 0)


class Malformed(ReportBase):
    GOOD = built("M1", "alpha", 0, 40.0, 42.0, 1000)

    def test_malformed_lines_are_skipped(self):  # covers: UREP-9
        bad = [
            "not json at all",
            "[1, 2, 3]",
            "42",
            "null",
            "{}",
            '{"kind": "agent_stop"',  # cut short
            json.dumps({"kind": "teleport", "milestone": "M1", "step": "alpha"}),
            json.dumps({"kind": "agent_stop", "milestone": "M9", "step": "odd", "tokens": "lots", "usage": None,
                        "role": None, "at": "yesterday", "other_sessions": "many"}),
            json.dumps({"kind": "agent_start", "milestone": 7, "step": ["x"], "usage": {"week_pct": "forty"},
                        "at": 12}),
            json.dumps({"kind": "step", "milestone": "M9", "step": "odd", "usage": "ok", "commit_files": "two",
                        "at": None, "lines_changed": None, "requirements": {}}),
            json.dumps({"kind": "agent_stop", "milestone": "M9", "step": "odd",
                        "tokens": {"input": "1", "output": None, "cache_read": [], "cache_write": {}}}),
            "",
        ]
        rows = [bad[0], self.GOOD[0], *bad[1:6], self.GOOD[1], *bad[6:], self.GOOD[2]]
        rep = self.report(rows)
        alpha = self.step(rep, "M1", "alpha")
        self.assertEqual(alpha["tokens_total"], 1000)
        self.assertAlmostEqual(alpha["week_pct"], 2.0)
        self.assertIsInstance(regista.format_usage_report(rep), str)
        self.cli()
        json.loads(self.cli("--json"))

    def test_bytes_that_arent_text_and_a_missing_newline(self):  # covers: UREP-9
        with open(self.usage_file(), "wb") as f:
            f.write(b"\xff\xfe\x00garbage\n")
            for r in self.GOOD:
                f.write(json.dumps(r).encode("utf-8") + b"\n")
            f.write(b'{"kind": "step", "milestone": "M1", "st')  # half a line, no newline
        rep = regista.usage_report(self.root, self.cfg)
        self.assertEqual(self.step(rep, "M1", "alpha")["tokens_total"], 1000)
        regista.format_usage_report(rep)
        self.cli()

    def test_never_raises(self):  # covers: UREP-9
        os.makedirs(self.usage_file())  # a folder where the file should be
        try:
            rep = regista.usage_report(self.root, self.cfg)
            regista.format_usage_report(rep)
            self.cli()
        finally:
            os.rmdir(self.usage_file())
        missing = os.path.join(STATE, "no-such-project")
        regista.format_usage_report(regista.usage_report(missing, {}))
        regista.format_usage_report(regista.usage_report(self.root, self.cfg, milestone="M42"))


STATUS_PLAN = """# Demo: plan

## M1 — Statistics
- [ ] 1. the statistics

## M2 — Charts
- [ ] 1. the charts
"""


class StatusLine(unittest.TestCase):
    def test_status_shows_the_current_milestones_cost(self):  # covers: UREP-10
        p = Project()
        self.addCleanup(p.cleanup)
        p.commit(".regista/plan.md", STATUS_PLAN, "plan")
        p.commit(".regista/specs/stats.json", json.dumps({
            "step": "stats", "milestone": "M1", "title": "Statistics",
            "requirements": [{"id": "STATS-1", "text": "mean", "verify": "test"}]}), "spec")
        rows = [start("M1", "stats", 0, 40.0),
                stop("M1", "stats", 5, 41.0, tokens=2000000),
                stop("M1", "stats", 6, 41.5, tokens=1200000, role="reviewer", agent_id="r1"),
                done("M1", "stats", 10, 42.5)]
        with open(os.path.join(p.root, ".regista", "usage.jsonl"), "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
        r = p.cli("status")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = [ln for ln in r.stdout.splitlines()
                 if pct(ln, 2.5) and re.search(r"3\.2\s*million|3[,.]200[,.]000|3\.2\s*M\b", ln)]
        self.assertEqual(len(lines), 1, "status should have one line with M1's cost (2.5% of the week, "
                                        f"3.2 million tokens):\n{r.stdout}")


class SaveSkill(unittest.TestCase):
    def test_save_puts_the_milestones_cost_in_latest_results(self):  # covers: UREP-11
        path = os.path.join(HERE, "..", "skills", "save", "SKILL.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("usage report", text)
        spots = [m.start() for m in re.finditer(r"usage report", text)]
        results = [m.start() for m in re.finditer(r"Latest results", text)]
        self.assertTrue(results, "the save skill should name the status file's 'Latest results'")
        self.assertTrue(any(abs(a - b) < 600 for a in spots for b in results),
                        "the save skill should say to put the usage report's milestone cost in 'Latest results'")
        self.assertRegex(text, r"regista(\.py)?[\"']?\s+usage report")


# --- usage.jsonl as local state, and a fleet worker's usage lines

def line(i, who):
    return json.dumps({**done("M2", f"{who}-{i}", i, 40.0 + i)}, sort_keys=True)


class UsageIsLocalState(unittest.TestCase):
    def test_usage_jsonl_is_ignored_by_git(self):  # covers: UREP-12
        p = Project()
        self.addCleanup(p.cleanup)
        p.write(".regista/usage.jsonl", line(1, "p") + "\n")
        r = subprocess.run(["git", "check-ignore", "-q", ".regista/usage.jsonl"], cwd=p.root)
        self.assertEqual(r.returncode, 0, ".regista/usage.jsonl should be ignored by git")
        self.assertNotIn("usage.jsonl", sh(p.root, "git", "status", "--porcelain", "--untracked-files=all"))


class WorkerUsage(unittest.TestCase):
    """A worker's usage.jsonl is kept with its other records and its lines join the project's, once each."""

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
        self.main_usage = os.path.join(self.p.root, ".regista", "usage.jsonl")
        # the project's own lines, one of which the worker also has
        self.shared = line(1, "w")
        self.project_lines = [line(1, "p"), line(2, "p"), self.shared]
        self.worker_lines = [self.shared, line(2, "w"), line(3, "w")]

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.p.root, capture_output=True)

    def fleet(self, *args):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        env.update(self.env)
        return subprocess.run([PY, TOOL, "fleet", *args], cwd=self.p.root, capture_output=True, text=True,
                              encoding="utf-8", env=env)

    def spawn_with_usage(self):
        self.p.hook("pre-tool", {**bash(f'python3 "{TOOL}" fleet spawn M2'), "session_id": "overseer-1",
                                 "permission_mode": "acceptEdits"}, extra_env=self.env)
        r = self.fleet("spawn", "M2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(jload(os.path.join(self.p.root, ".regista", "fleet", "workers.json"))["m2"]["session_id"])
        with open(self.main_usage, "a", encoding="utf-8") as f:
            f.writelines(ln + "\n" for ln in self.project_lines)
        with open(os.path.join(self.wt, ".regista", "usage.jsonl"), "w", encoding="utf-8") as f:
            f.writelines(ln + "\n" for ln in self.worker_lines)
        fp = os.path.join(self.wt, "src", "pages.py")
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write("pages = 1\n")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "feat: pages")

    def assert_usage_joined(self):
        self.assertFalse(os.path.exists(self.wt))
        kept = os.path.join(self.kept, "usage.jsonl")
        self.assertTrue(os.path.isfile(kept), "the worker's usage.jsonl wasn't kept under .regista/fleet/logs/M2/m2/")
        with open(kept, encoding="utf-8") as f:
            self.assertEqual(f.read().splitlines(), self.worker_lines)
        with open(self.main_usage, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        self.assertIn(self.project_lines[0], lines, "the project's lines were lost")
        i = lines.index(self.project_lines[0])
        self.assertEqual(lines[i:i + len(self.project_lines)], self.project_lines, "the project's lines were changed")
        for ln in self.worker_lines:
            self.assertEqual(lines.count(ln), 1, f"the worker's line should be in the project's usage.jsonl once: {ln}")

    def test_merged_and_removed_worker_adds_its_usage_lines(self):  # covers: UREP-12
        self.spawn_with_usage()
        r = self.fleet("merge", "m2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.fleet("remove", "m2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assert_usage_joined()

    def test_removed_unmerged_worker_adds_its_usage_lines(self):  # covers: UREP-12
        self.spawn_with_usage()
        r = self.fleet("remove", "m2", "--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assert_usage_joined()


if __name__ == "__main__":
    unittest.main()
