"""Tests for the estimate before a build: usage_estimate(root, cfg, target=None), format_estimate(est) and
`regista.py estimate [M<n> | all] [--json]`, fed with hand-written .regista/usage.jsonl lines (shaped like the ones
the hooks write, as in tests/test_usage_report.py), other projects listed in the machine-wide usage-projects list,
and the local fake usage server. Also: the /regista:estimate skill, the build skill's offer, and the weekly limit
during a build-all run (the commit hook's note and the Stop hook). The end-to-end check is in
tests/e2e/test_usage_estimate.py.

Nothing here reaches the network or the real home folder: HOME, XDG_STATE_HOME and XDG_CACHE_HOME are temporary,
the usage address is the fake server on 127.0.0.1 (or a closed port there) and the credentials file holds a fake
token.

Run: python3 -m unittest tests.test_usage_estimate (from the repository root) or discover -s tests"""
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_regista import Project, bash, sh  # noqa: E402
from fake_usage import (SESSION_RESETS_AT, WEEK_RESETS_AT, WEEK_RESETS_TEXT_BUCHAREST, FakeUsageServer,  # noqa: E402
                        closed_port_url, usage_reply, write_credentials)
import regista  # noqa: E402  (test_regista put scripts/ on the path)

NOT_PLAN_VARS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")
M = 1_000_000

# M1 is built; M2 has 3 unticked steps (one ticked, one with a continuation line); M3 has 2. Gate bullets and the
# out-of-scope list are not steps.
PLAN = """# Demo: plan

## M1 — Statistics
Gate:
- `regista.py stats` prints the mean.

Steps:
- [x] 1. the mean
- [x] 2. the median

## M2 — Charts
Gate:
- a chart is drawn

Steps:
- [ ] 1. bar charts
- [x] 2. axis labels
- [ ] 3. line charts, with a long description that
   goes on to a second line
- [ ] 4. legends

## M3 — Export
Steps:
- [ ] 1. CSV
- [ ] 2. PDF
"""


# --- hand-written records, shaped like the ones the hooks write

def at(minute):
    """A local ISO time with offset, `minute` minutes after 10:00 on 3 November 2026."""
    return f"2026-11-03T{10 + minute // 60:02d}:{minute % 60:02d}:00+02:00"


def reading(week, session=10.0):
    known = week is not None
    return {"state": "ok" if known else "unknown", "session_pct": session if known else None, "week_pct": week,
            "session_resets_at": SESSION_RESETS_AT, "week_resets_at": WEEK_RESETS_AT}


def tokens_of(total):
    return {"input": 1, "output": 2, "cache_write": 3, "cache_read": total - 6}


def history(steps, ms="M1", prefix="s", week=40.0, other=0):
    """Records for past steps built one after the other. steps: (tokens per role, rise in the week %, minutes from
    the first agent's start to the step's commit). Each step starts with an agent_start, has one agent_stop per
    role, and ends with its step record."""
    rows, t, w = [], 0, week
    for i, (roles, rise, minutes) in enumerate(steps, 1):
        name = f"{prefix}{i}"
        first = next(iter(roles))
        rows.append({"kind": "agent_start", "at": at(t), "milestone": ms, "step": name, "session_id": "s1",
                     "usage": reading(w), "other_sessions": other, "agent_id": f"{name}-0", "role": first,
                     "agent_type": f"regista:{first}"})
        for j, (role, tok) in enumerate(roles.items()):
            rows.append({"kind": "agent_stop", "at": at(t + 1), "milestone": ms, "step": name, "session_id": "s1",
                         "usage": reading(w), "other_sessions": other, "agent_id": f"{name}-{j}", "role": role,
                         "agent_type": f"regista:{role}", "tokens": tokens_of(tok), "model": "claude-x",
                         "seconds": 60.0})
        w = w + rise
        rows.append({"kind": "step", "at": at(t + minutes), "milestone": ms, "step": name, "session_id": "s1",
                     "usage": reading(w), "other_sessions": other, "commit_files": 2, "lines_changed": 20,
                     "requirements": 3})
        t += minutes + 1
    return rows


def simple(tokens_m, rises, minutes=None):
    """Steps with a builder only: tokens in millions, rises in % of the week, minutes (10 each by default)."""
    minutes = minutes or [10] * len(tokens_m)
    return [({"builder": int(tok * M)}, rise, mins) for tok, rise, mins in zip(tokens_m, rises, minutes)]


# Seven past steps whose totals are 1, 2, 2, 3, 4, 4 and 5 million tokens (in another order), so the 25th and 75th
# percentiles are 2 and 4 million whichever usual percentile method is used. Each raised the week by its tokens
# over 2 million (2 million tokens per 1%). Durations 10, 20, 20, 30, 40, 40 and 50 minutes: 20 and 40.
# Per role, the medians are builder 1.8, test-writer 0.6 and reviewer 0.5 million (not the median step's split,
# not the means).
SEVEN = [
    ({"builder": 1_800_000, "test-writer": 600_000, "reviewer": 600_000}, 1.5, 30),
    ({"builder": 600_000, "test-writer": 200_000, "reviewer": 200_000}, 0.5, 10),
    ({"builder": 3_000_000, "test-writer": 500_000, "reviewer": 500_000}, 2.0, 40),
    ({"builder": 1_000_000, "test-writer": 800_000, "reviewer": 200_000}, 1.0, 20),
    ({"builder": 1_200_000, "test-writer": 400_000, "reviewer": 400_000}, 1.0, 20),
    ({"builder": 2_500_000, "test-writer": 1_000_000, "reviewer": 500_000}, 2.0, 40),
    ({"builder": 3_500_000, "test-writer": 1_000_000, "reviewer": 500_000}, 2.5, 50),
]

RANGE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?) to (\d+(?:\.\d+)?)\s*% of your week")


def has_pct(text, value):
    """Whether `value` percent appears in text, with one decimal or as few as needed (80%, 80.0 %)."""
    forms = {f"{value:.1f}", f"{value:g}"}
    return any(re.search(r"(?<![\d.])" + re.escape(f) + r"\s*%", text) for f in forms)


def first_line(text):
    return next(ln for ln in text.splitlines() if ln.strip())


def all_text(answer):
    """Every string in a hook's JSON answer, joined (the note may be in any of its fields)."""
    if answer is None:
        return ""
    if isinstance(answer, str):
        return answer
    if isinstance(answer, dict):
        return "\n".join(all_text(v) for v in answer.values())
    if isinstance(answer, list):
        return "\n".join(all_text(v) for v in answer)
    return ""


class UsageEnv(unittest.TestCase):
    """Temporary state, cache and credentials, the fake usage server, and Bucharest time, for each test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="regista-uest-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_home = os.path.join(self.tmp, "state")
        self.srv = FakeUsageServer(reply=usage_reply(37.0, 80.0)).start()
        self.addCleanup(self.srv.stop)
        creds = write_credentials(os.path.join(self.tmp, "credentials.json"))
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.state_home,
                                           "XDG_CACHE_HOME": os.path.join(self.tmp, "cache"),
                                           "REGISTA_USAGE_URL": self.srv.url, "REGISTA_CREDENTIALS": creds,
                                           "REGISTA_USAGE_CACHE_SECONDS": "0", "TZ": "Europe/Bucharest"})
        env.start()
        self.addCleanup(time.tzset)
        self.addCleanup(env.stop)
        for k in NOT_PLAN_VARS:
            os.environ.pop(k, None)
        time.tzset()

    def week(self, pct):
        self.srv.configure(reply=usage_reply(37.0, pct), replies=None)

    def usage_unknown(self):
        os.environ["REGISTA_USAGE_URL"] = closed_port_url()


class EstimateBase(UsageEnv):
    @classmethod
    def setUpClass(cls):
        cls.p = Project()
        cls.p.commit(".regista/plan.md", PLAN, "plan")
        cls.root = cls.p.root
        cls.cfg = regista.load_config(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.p.cleanup()

    def setUp(self):
        super().setUp()
        if os.path.exists(self.usage_file()):
            os.remove(self.usage_file())

    def usage_file(self):
        return os.path.join(self.root, ".regista", "usage.jsonl")

    def write_records(self, rows, root=None):
        path = os.path.join(root, ".regista", "usage.jsonl") if root else self.usage_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")

    def other_project(self, rows, name):
        """Another project on this machine: a folder with its own .regista/usage.jsonl."""
        root = os.path.join(self.tmp, "others", name)
        self.write_records(rows, root)
        return root

    def list_projects(self, entries):
        path = os.path.join(self.state_home, "regista", "usage-projects.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(entries if isinstance(entries, str) else json.dumps(entries))

    def estimate(self, target=None, cfg=None, rows=None):
        if rows is not None:
            self.write_records(rows)
        est = regista.usage_estimate(self.root, cfg if cfg is not None else self.cfg, target=target)
        self.assertIsInstance(est, dict)
        return est

    def cli(self, *args):
        r = self.p.cli("estimate", *args)
        self.assertNotIn("Traceback", r.stdout + r.stderr, r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout


# --- UEST-1: the steps left

class StepsLeft(EstimateBase):
    def test_counts_the_unticked_steps_of_the_target(self):  # covers: UEST-1
        rows = history(SEVEN)
        self.write_records(rows)
        nxt = self.estimate()
        self.assertEqual((nxt["target"], nxt["steps_left"]), ("M2", 3))  # the next milestone with unticked steps
        self.assertEqual(self.estimate("M2")["steps_left"], 3)
        self.assertEqual(self.estimate("M3")["steps_left"], 2)
        every = self.estimate("all")
        self.assertEqual((every["target"], every["steps_left"]), ("all", 5))
        self.assertEqual(self.estimate("M1")["steps_left"], 0)
        data = json.loads(self.cli("--json"))
        self.assertEqual((data["target"], data["steps_left"]), ("M2", 3))
        self.assertEqual(json.loads(self.cli("all", "--json"))["steps_left"], 5)
        self.assertEqual(json.loads(self.cli("M3", "--json"))["steps_left"], 2)

    def test_a_milestone_not_in_the_plan_names_the_plans_milestones(self):  # covers: UEST-1
        out = self.cli("M9")
        for ms in ("M1", "M2", "M3"):
            self.assertIn(ms, out)
        self.assertNotRegex(out, r"Traceback|[{}]")


# --- UEST-2: which past steps

class WhichPastSteps(EstimateBase):
    def test_enough_steps_here_rest_on_this_project_only(self):  # covers: UEST-2
        # four steps (the minimum) of 2, 2, 3 and 3 million tokens here; much bigger steps elsewhere
        big = self.other_project(history(simple([50, 60, 70], [25.0, 30.0, 35.0])), "big")
        self.list_projects([big, self.root])
        est = self.estimate("M2", rows=history(simple([2, 3, 2, 3], [1.0, 1.5, 1.0, 1.5])))
        self.assertEqual(regista.ESTIMATE_MIN_STEPS, 4)
        self.assertEqual(est["source"], "project")
        self.assertEqual(est["based_on"], {"project": 4, "other_projects": 0})
        self.assertEqual((est["tokens_low"], est["tokens_high"]), (6 * M, 9 * M))

    def test_too_few_here_adds_the_other_projects(self):  # covers: UEST-2
        # 1, 3 and 5 million here; 2 and 4 million in each of two other projects. Together: 1, 2, 2, 3, 4, 4, 5.
        b = self.other_project(history(simple([2, 4], [1.0, 2.0])), "b")
        c = self.other_project(history(simple([4, 2], [2.0, 1.0])), "c")
        empty = os.path.join(self.tmp, "others", "never-built")
        os.makedirs(empty)
        self.list_projects([self.root, b, empty, c])  # this project in the list is not counted twice
        est = self.estimate("M2", rows=history(simple([1, 3, 5], [0.5, 1.5, 2.5])))
        self.assertEqual(est["source"], "machine")
        self.assertEqual(est["based_on"], {"project": 3, "other_projects": 4})
        self.assertEqual((est["tokens_low"], est["tokens_high"]), (6 * M, 12 * M))
        self.assertAlmostEqual(est["tokens_per_pct"], 2 * M, delta=1)  # the same in every project

    def test_too_few_here_and_none_elsewhere_still_uses_them(self):  # covers: UEST-2
        est = self.estimate("M2", rows=history(simple([1, 3, 5], [0.5, 1.5, 2.5])))
        self.assertIn(est["source"], ("project", "machine"))
        self.assertEqual(est["based_on"], {"project": 3, "other_projects": 0})
        self.assertEqual((est["tokens_low"], est["tokens_high"]), (3 * M, 15 * M))

    def test_no_past_steps_anywhere_gives_the_rough_default(self):  # covers: UEST-2
        empty = os.path.join(self.tmp, "others", "never-built")
        os.makedirs(os.path.join(empty, ".regista"))
        self.list_projects([empty])
        # an agent that never reached a commit is not a past step
        only_agents = [r for r in history(SEVEN[:1]) if r["kind"] != "step"]
        est = self.estimate("M2", rows=only_agents)
        self.assertEqual(est["source"], "default")
        self.assertEqual(est["based_on"], {"project": 0, "other_projects": 0})
        self.assertEqual(regista.DEFAULT_STEP_TOKENS, (4_000_000, 12_000_000))
        self.assertEqual(regista.DEFAULT_TOKENS_PER_PCT, 100_000_000)
        self.assertEqual(regista.DEFAULT_STEP_MINUTES, (20, 60))
        self.assertEqual((est["tokens_low"], est["tokens_high"]), (12 * M, 36 * M))
        self.assertEqual(est["tokens_per_pct"], 100_000_000)
        self.assertAlmostEqual(est["pct_low"], 0.12)
        self.assertAlmostEqual(est["pct_high"], 0.36)
        self.assertEqual((est["minutes_low"], est["minutes_high"]), (60, 180))
        text = regista.format_estimate(est).lower()
        self.assertRegex(text, r"\b(no|not any|nothing)\b[^.\n]*record")
        self.assertIn("rough", text)


# --- UEST-3: tokens

class Tokens(EstimateBase):
    def test_quartiles_times_steps_left_and_median_per_role(self):  # covers: UEST-3
        est = self.estimate("M2", rows=history(SEVEN))
        self.assertEqual(est["based_on"], {"project": 7, "other_projects": 0})
        self.assertEqual((est["tokens_low"], est["tokens_high"]), (2 * M * 3, 4 * M * 3))
        self.assertEqual(est["tokens_by_role"], {"builder": 1_800_000, "test-writer": 600_000, "reviewer": 500_000})
        for v in (est["tokens_low"], est["tokens_high"], *est["tokens_by_role"].values()):
            self.assertIsInstance(v, int)
        every = self.estimate("all")
        self.assertEqual((every["tokens_low"], every["tokens_high"]), (2 * M * 5, 4 * M * 5))

    def test_fewer_than_four_steps_use_the_lowest_and_highest(self):  # covers: UEST-3
        est = self.estimate("M3", rows=history(simple([9, 1, 5], [4.5, 0.5, 2.5])))
        self.assertEqual((est["tokens_low"], est["tokens_high"]), (1 * M * 2, 9 * M * 2))


# --- UEST-4: tokens per 1% and the percentages

class Percentages(EstimateBase):
    def test_tokens_per_pct_from_the_report_and_the_range(self):  # covers: UEST-4
        est = self.estimate("M2", rows=history(SEVEN))
        report = regista.usage_report(self.root, self.cfg)
        self.assertAlmostEqual(est["tokens_per_pct"], report["tokens_per_pct"], delta=1)
        self.assertAlmostEqual(est["tokens_per_pct"], 2 * M, delta=1)
        self.assertAlmostEqual(est["pct_low"], 3.0)
        self.assertAlmostEqual(est["pct_high"], 6.0)

    def test_no_tokens_per_pct_in_the_report_uses_the_default(self):  # covers: UEST-4
        # every step ran beside other sessions: the report has no tokens per 1%
        est = self.estimate("M2", rows=history(SEVEN, other=2))
        self.assertIsNone(regista.usage_report(self.root, self.cfg)["tokens_per_pct"])
        self.assertEqual(est["tokens_per_pct"], 100_000_000)
        self.assertAlmostEqual(est["pct_low"], 0.06)
        self.assertAlmostEqual(est["pct_high"], 0.12)


# --- UEST-5: how long

class Minutes(EstimateBase):
    def test_quartiles_of_the_durations_times_steps_left(self):  # covers: UEST-5
        est = self.estimate("M2", rows=history(SEVEN))
        self.assertEqual((est["minutes_low"], est["minutes_high"]), (60, 120))

    def test_fewer_than_four_steps_use_the_shortest_and_longest(self):  # covers: UEST-5
        est = self.estimate("M2", rows=history(simple([1, 3, 5], [0.5, 1.5, 2.5], minutes=[30, 10, 50])))
        self.assertEqual((est["minutes_low"], est["minutes_high"]), (30, 150))

    def test_no_durations_use_the_default_minutes(self):  # covers: UEST-5
        rows = [{k: v for k, v in r.items() if k != "at"} for r in history(SEVEN)]  # no times: no durations
        self.write_records(rows)
        steps = regista.usage_report(self.root, self.cfg)["steps"]
        self.assertTrue(steps and all(s["seconds"] is None for s in steps), "setup: steps without durations")
        est = self.estimate("M2")
        self.assertEqual((est["minutes_low"], est["minutes_high"]), (20 * 3, 60 * 3))


# --- UEST-6: whether it fits

class Fits(EstimateBase):
    def setUp(self):
        super().setUp()
        self.write_records(history(SEVEN))  # M2 costs 3 to 6% of the week

    def fits(self, week, cfg=None):
        self.week(week)
        return self.estimate("M2", cfg=cfg)

    def test_against_the_default_stop_at_90(self):  # covers: UEST-6
        est = self.fits(84.0)
        self.assertEqual(est["stop_at"], 90)
        self.assertEqual(est["week"]["state"], "ok")
        self.assertAlmostEqual(est["week"]["pct"], 84.0)
        self.assertEqual(datetime.fromisoformat(est["week"]["resets_at"]), datetime.fromisoformat(WEEK_RESETS_AT))
        self.assertAlmostEqual(est["session"]["pct"], 37.0)
        self.assertEqual(est["fits"], "yes")  # 84 + 6 = 90: at most the stop
        self.assertEqual(self.fits(80.0)["fits"], "yes")
        self.assertEqual(self.fits(85.0)["fits"], "maybe")  # 88 to 91
        self.assertEqual(self.fits(87.0)["fits"], "maybe")  # 87 + 3 = 90: not above the stop
        self.assertEqual(self.fits(88.0)["fits"], "no")  # 91 to 94

    def test_against_a_configured_stop_at(self):  # covers: UEST-6
        cfg = {**self.cfg, "usage": {"stop_at_weekly_pct": 50}}
        self.assertEqual(self.fits(44.0, cfg)["stop_at"], 50)
        self.assertEqual(self.fits(44.0, cfg)["fits"], "yes")
        self.assertEqual(self.fits(45.0, cfg)["fits"], "maybe")
        self.assertEqual(self.fits(48.0, cfg)["fits"], "no")

    def test_unknown_usage(self):  # covers: UEST-6
        self.usage_unknown()
        est = self.estimate("M2")
        self.assertEqual(est["fits"], "unknown")
        self.assertAlmostEqual(est["pct_low"], 3.0)  # the cost is still there


# --- UEST-7: the text

class Text(EstimateBase):
    def text(self, target="M2", rows=None):
        est = self.estimate(target, rows=rows if rows is not None else history(SEVEN))
        text = regista.format_estimate(est)
        self.assertIsInstance(text, str)
        return text

    def test_cost_first_then_week_reset_fit_time_tokens_and_past_steps(self):  # covers: UEST-7
        self.week(80.0)
        text = self.text()
        head = first_line(text)
        self.assertIn("M2", head)
        m = RANGE.search(head)
        self.assertTrue(m, f"the first line should give the cost as a range of the week:\n{text}")
        self.assertEqual((float(m.group(1)), float(m.group(2))), (3.0, 6.0))
        self.assertRegex(text, r"\b3 steps\b")
        self.assertTrue(has_pct(text, 80), f"the week used:\n{text}")
        self.assertTrue(has_pct(text, 10), f"what is left before the stop at 90%:\n{text}")
        self.assertTrue(has_pct(text, 90), text)
        self.assertIn(WEEK_RESETS_TEXT_BUCHAREST, text)
        self.assertIn("fit", text.lower())
        self.assertRegex(text, r"\b(minutes?|hours?)\b")
        self.assertTrue(any("token" in ln and "million" in ln and re.search(r"(?<![\d.])12\b", ln)
                            for ln in text.splitlines()), f"the tokens range (6 to 12 million):\n{text}")
        self.assertTrue(any(re.search(r"(?<![\d.])7\b", ln) and "step" in ln for ln in text.splitlines()[1:]),
                        f"how many past steps it rests on (7):\n{text}")
        self.assertNotRegex(text, r"[{}]|None|null")

    def test_past_steps_from_this_and_other_projects(self):  # covers: UEST-7
        b = self.other_project(history(simple([2, 4], [1.0, 2.0])), "b")
        c = self.other_project(history(simple([4, 2], [2.0, 1.0])), "c")
        self.list_projects([b, c])
        text = self.text("M3", rows=history(simple([1, 3, 5], [0.5, 1.5, 2.5])))  # 2 steps left
        self.assertIn("other project", text.lower())
        self.assertTrue(any(re.search(r"(?<![\d.])3\b", ln) for ln in text.splitlines()[1:]), text)
        self.assertRegex(text, r"(?<![\d.])4\b[^\n]*other project|other project[^\n]*(?<![\d.])4\b")

    def test_small_percentages_keep_enough_decimals_to_differ(self):  # covers: UEST-7
        text = self.text(rows=history(SEVEN, other=1))  # 0.06 to 0.12% (the default tokens per 1%)
        m = RANGE.search(first_line(text))
        self.assertTrue(m, text)
        low, high = m.group(1), m.group(2)
        self.assertNotEqual(low, high, text)
        self.assertGreater(float(low), 0, text)
        self.assertAlmostEqual(float(low), 0.06, delta=0.006)
        self.assertAlmostEqual(float(high), 0.12, delta=0.012)

    def test_unknown_usage_still_gives_the_cost(self):  # covers: UEST-7
        self.usage_unknown()
        text = self.text()
        m = RANGE.search(first_line(text))
        self.assertTrue(m, text)
        self.assertEqual((float(m.group(1)), float(m.group(2))), (3.0, 6.0))
        self.assertIn("unknown", text.lower())

    def test_the_cli_prints_the_text(self):  # covers: UEST-7
        self.week(80.0)
        self.write_records(history(SEVEN))
        out = self.cli("M2")
        self.assertTrue(RANGE.search(first_line(out)), out)
        self.assertIn(WEEK_RESETS_TEXT_BUCHAREST, out)


# --- UDOC-3 to UDOC-6: fixes to the estimate's text

def outside_brackets(text):
    """The text with every bracketed part (round or square, nested or not) removed."""
    out, depth = [], 0
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]" and depth:
            depth -= 1
        elif not depth:
            out.append(ch)
    return "".join(out)


def deepest_bracket(text):
    depth = deepest = 0
    for ch in text:
        if ch in "([":
            depth += 1
            deepest = max(deepest, depth)
        elif ch in ")]" and depth:
            depth -= 1
    return deepest


class TextFixes(EstimateBase):
    def est(self, target="M2", rows=None):
        return self.estimate(target, rows=rows if rows is not None else history(SEVEN))

    def text_of(self, est):
        text = regista.format_estimate(est)
        self.assertIsInstance(text, str)
        return text

    def test_unknown_usage_gives_the_reason_as_its_own_sentence(self):  # covers: UDOC-3
        self.usage_unknown()
        reason = (regista.read_usage(self.cfg, fresh=True).get("reason") or "").strip().rstrip(".")
        self.assertTrue(reason, "setup: an unreachable usage address should give a reason")
        text = self.text_of(self.est())
        self.assertLessEqual(deepest_bracket(text), 1, f"brackets nested inside brackets:\n{text}")
        flat = re.sub(r"\s+", " ", outside_brackets(text))
        body = re.escape(reason[1:])
        self.assertRegex(flat, r"(?:^|[.!?:] )" + "[" + re.escape(reason[0].upper() + reason[0].lower()) + "]" + body
                         + r"[.!?]", f"the reason ({reason!r}) should be a sentence of its own, not in brackets:\n{text}")

    def test_whole_millions_have_no_decimal(self):  # covers: UDOC-4
        text = self.text_of(self.est())  # 6 to 12 million tokens for M2
        self.assertRegex(text, r"(?<![\d.])6 to 12 million tokens", text)
        self.assertNotRegex(text, r"(?<![\d.])\d+\.0 million", text)

    def test_other_amounts_have_one_decimal(self):  # covers: UDOC-4
        est = self.est()
        est.update(tokens_low=2_500_000, tokens_high=8_000_000)
        text = self.text_of(est)
        self.assertRegex(text, r"(?<![\d.])2\.5( million)? to 8 million tokens", text)
        self.assertNotRegex(text, r"(?<![\d.])\d+\.\d\d+ million", text)  # never two decimals
        self.assertNotRegex(text, r"(?<![\d.])\d+\.0 million", text)

    def test_a_cost_too_small_to_show_says_less_than(self):  # covers: UDOC-5
        est = self.est()
        est.update(pct_low=0.0001, pct_high=0.0004)
        text = self.text_of(est)
        self.assertIn("less than 0.001% of your week", text)
        self.assertNotRegex(text.lower(), r"about 0(\.0+)?\s*%")
        est.update(pct_low=0.0, pct_high=0.0)
        text = self.text_of(est)
        self.assertIn("less than 0.001% of your week", text)
        self.assertNotRegex(text.lower(), r"about 0(\.0+)?\s*%")

    def test_a_low_end_of_zero_is_not_0_0(self):  # covers: UDOC-5
        est = self.est()
        est.update(pct_low=0.0, pct_high=0.5)
        head = first_line(self.text_of(est))
        self.assertNotRegex(head, r"(?<![\d.])0\.0(?!\d)", head)
        self.assertRegex(head, r"(?<![\d.])0\.5\s*%", head)

    def test_an_unreadable_reset_time_leaves_out_only_that_part(self):  # covers: UDOC-6
        self.week(80.0)
        est = self.est()
        est["week"] = {**est["week"], "resets_at": "sometime next week"}
        text = self.text_of(est)
        m = RANGE.search(first_line(text))
        self.assertTrue(m, f"the cost line should still be printed:\n{text}")
        self.assertEqual((float(m.group(1)), float(m.group(2))), (3.0, 6.0))
        self.assertNotIn("sometime next week", text)
        self.assertTrue(has_pct(text, 80), f"the week's use should still be there:\n{text}")
        self.assertIn("fit", text.lower())
        self.assertRegex(text, r"(?<![\d.])12 million", text)

    def test_other_unreadable_parts_leave_the_cost_line(self):  # covers: UDOC-6
        self.week(80.0)
        for broken in ({"minutes_low": "soon", "minutes_high": None}, {"tokens_by_role": "lots"},
                       {"based_on": None}, {"session": "x"}):
            with self.subTest(broken=broken):
                est = {**self.est(), **broken}
                text = self.text_of(est)
                m = RANGE.search(first_line(text))
                self.assertTrue(m, f"the cost line should still be printed:\n{text}")
                self.assertEqual((float(m.group(1)), float(m.group(2))), (3.0, 6.0))
                self.assertIn(WEEK_RESETS_TEXT_BUCHAREST, text)


# --- UEST-13: never raises

class NeverRaises(EstimateBase):
    def check(self, target="M2"):
        est = regista.usage_estimate(self.root, self.cfg, target=target)
        self.assertIsInstance(est, dict)
        self.assertIsInstance(regista.format_estimate(est), str)
        self.cli(target)
        return est

    def test_unreadable_records_are_skipped(self):  # covers: UEST-13
        good = history(simple([2, 3, 2, 3], [1.0, 1.5, 1.0, 1.5]))
        self.write_records(["not json", "[1, 2]", "null", '{"kind": "step"', *good,
                            json.dumps({"kind": "step", "milestone": 7, "step": ["x"], "usage": "ok",
                                        "commit_files": "two", "at": None})])
        est = self.check()
        self.assertEqual(est["based_on"]["project"], 4)
        os.remove(self.usage_file())
        os.makedirs(self.usage_file())  # a folder where the file should be
        try:
            self.check()
        finally:
            os.rmdir(self.usage_file())

    def test_unreadable_other_projects_are_skipped(self):  # covers: UEST-13
        self.write_records(history(simple([1, 3], [0.5, 1.5])))
        good = self.other_project(history(simple([2, 4], [1.0, 2.0])), "good")
        garbage = self.other_project(["\x00 not json", "{}", "42"], "garbage")
        folder = os.path.join(self.tmp, "others", "folder-record")
        os.makedirs(os.path.join(folder, ".regista", "usage.jsonl"))
        a_file = os.path.join(self.tmp, "a-file")
        with open(a_file, "w", encoding="utf-8") as f:
            f.write("x")
        self.list_projects([42, None, {"root": good}, "", "relative/path", os.path.join(self.tmp, "gone"), a_file,
                            garbage, folder, good])
        est = self.check()
        self.assertEqual(est["based_on"]["project"], 2)
        self.assertGreaterEqual(est["based_on"]["other_projects"], 2)
        for broken in ("not json", '{"projects": 1}', "null", ""):
            self.list_projects(broken)
            self.assertEqual(self.check()["based_on"]["project"], 2)

    def test_a_missing_plan(self):  # covers: UEST-13
        p = Project()
        self.addCleanup(p.cleanup)
        plan = os.path.join(p.root, ".regista", "plan.md")
        if os.path.exists(plan):
            os.remove(plan)
        cfg = regista.load_config(p.root)
        for target in (None, "M1", "all"):
            est = regista.usage_estimate(p.root, cfg, target=target)
            self.assertIsInstance(regista.format_estimate(est), str)
        r = p.cli("estimate")
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        bare = os.path.join(self.tmp, "bare")
        os.makedirs(bare)
        self.assertIsInstance(regista.format_estimate(regista.usage_estimate(bare, {})), str)


# --- UEST-9: the skills

def skill(name):
    with open(os.path.join(HERE, "..", "skills", name, "SKILL.md"), encoding="utf-8") as f:
        return f.read()


class Skills(unittest.TestCase):
    def test_the_estimate_skill_and_the_build_skills_offer(self):  # covers: UEST-9
        text = skill("estimate")
        front = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
        self.assertTrue(front, "the estimate skill needs front matter")
        self.assertRegex(front.group(1), r"(?m)^name:\s*[\"']?(regista:)?estimate[\"']?\s*$")
        self.assertRegex(front.group(1), r"(?m)^description:\s*\S")
        body = text[front.end():]
        self.assertRegex(body, r"regista(\.py)?[\"']?\s+estimate")
        self.assertIn("all", body)
        self.assertRegex(body, r"M<n>|M\d")
        self.assertRegex(body.lower(), r"plain")
        self.assertRegex(body.lower(), r"cost[^.\n]*first|first[^.\n]*cost")
        build = skill("build")
        spots = [m.start() for m in re.finditer(r"estimate", build)]
        self.assertTrue(spots, "the build skill should offer the estimate")
        for phrase in (r"build all", r"build M<n>|build M\d|a milestone"):
            near = [m.start() for m in re.finditer(phrase, build)]
            self.assertTrue(any(abs(a - b) < 800 for a in spots for b in near),
                            f"the build skill should offer the estimate before `{phrase}` starts")
        self.assertRegex(build.lower(), r"estimate[^\n]*before|before[^\n]*estimate")


# --- UEST-10, UEST-11, UEST-12: the weekly limit during a build-all run

HOOK_PLAN = """# Demo: plan

## M1 — Stats
Steps:
- [ ] 1. the mean

## M2 — CLI
Steps:
- [ ] 1. calc command
"""

STATS_SPEC = {"step": "stats", "milestone": "M1", "title": "Statistics",
              "requirements": [{"id": "STATS-1", "text": "the mean", "verify": "test"}]}


class BuildAllWeek(UsageEnv):
    def setUp(self):
        super().setUp()
        self.p = Project(gates=[{"name": "tests", "run": "echo ok", "when": ["commit"]}],
                         review={"required_for": []},
                         requirements={"enforce_coverage": False, "milestone_needs_e2e": False})
        self.addCleanup(self.p.cleanup)
        self.p.commit(".regista/plan.md", HOOK_PLAN, "plan")
        self.p.commit(".regista/specs/stats.json", json.dumps(STATS_SPEC), "spec")
        r = self.p.cli("run", "start")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def commit_hook(self, week):
        self.week(week)
        self.p.write("src/stats.py", f"def mean(xs):\n    return sum(xs) / len(xs)  # {week}\n")
        self.p.write("tests/test_stats.py", "# covers: STATS-1\nx = 1\n")
        sh(self.p.root, "git", "add", "-A")
        out = self.p.hook("pre-tool", {**bash('git commit -m "feat: mean"'), "session_id": "s1"})
        self.assertNotEqual((out or {}).get("hookSpecificOutput", {}).get("permissionDecision"), "deny", out)
        return all_text(out)

    def stop_hook(self, week=None):
        if week is not None:
            self.week(week)
        return self.p.hook("stop", {"session_id": "s1"}) or {}

    def meter_off(self):
        path = os.path.join(self.p.root, ".regista", "config.json")
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["usage"] = {"enabled": False}
        self.p.commit(".regista/config.json", json.dumps(cfg), "meter off")

    def test_commit_notes_the_week_at_the_warning_level(self):  # covers: UEST-10
        quiet = self.commit_hook(50.0)
        self.assertNotIn(WEEK_RESETS_TEXT_BUCHAREST, quiet)
        note = self.commit_hook(75.0)
        self.assertTrue(has_pct(note, 75), f"the note should give the week's use:\n{note}")
        self.assertIn(WEEK_RESETS_TEXT_BUCHAREST, note)

    def test_commit_at_the_stop_level_says_to_save_and_end_the_run(self):  # covers: UEST-10
        note = self.commit_hook(90.0)
        low = note.lower()
        self.assertTrue(has_pct(note, 90), note)
        self.assertIn(WEEK_RESETS_TEXT_BUCHAREST, note)
        self.assertIn("save", low)
        self.assertRegex(note, r"run end[^\n]*--reason")
        self.assertIn("week", low)
        self.assertRegex(low, r"no more|no further|no new|not start|n't start")

    def test_stop_hook_at_the_stop_level_doesnt_send_it_back_to_work(self):  # covers: UEST-11
        out = self.stop_hook(92.0)  # below the stop it says to carry on (see the UEST-12 tests)
        text = all_text(out)
        self.assertNotIn("carry on", text.lower(), f"it shouldn't send the session back to work:\n{text}")
        self.assertIn("save", text.lower())
        self.assertIn("run end", text)
        self.assertIn(WEEK_RESETS_TEXT_BUCHAREST, text)

    def test_meter_off_behaves_as_before(self):  # covers: UEST-12
        self.meter_off()
        note = self.commit_hook(95.0)
        self.assertNotIn(WEEK_RESETS_TEXT_BUCHAREST, note)
        self.assertFalse(has_pct(note, 95), note)
        out = self.stop_hook(95.0)
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("carry on", all_text(out).lower())

    def test_unknown_usage_behaves_as_before(self):  # covers: UEST-12
        self.usage_unknown()
        note = self.commit_hook(95.0)
        self.assertNotIn(WEEK_RESETS_TEXT_BUCHAREST, note)
        self.assertNotRegex(note.lower(), r"weekly limit|run end")
        out = self.stop_hook()
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("carry on", all_text(out).lower())

    def test_a_second_stop_at_the_limit_ends_the_run_and_lets_the_session_stop(self):  # covers: UDOC-7
        self.week(92.0)
        self.p.hook("stop", {"session_id": "s1"})  # the first stop says to save and end the run
        out = self.p.hook("stop", {"session_id": "s1", "stop_hook_active": True}) or {}  # no new commit since
        self.assertNotEqual(out.get("decision"), "block", f"it should let the session stop:\n{out}")
        state = regista.load_json(regista.run_path(self.p.root), {})
        self.assertFalse(state.get("active"), f"the run should have ended:\n{state}")
        reason = all_text(state)
        self.assertRegex(reason.lower(), r"week", f"the run's reason should name the weekly limit:\n{state}")
        self.assertIn("limit", reason.lower(), state)
        self.assertIn(WEEK_RESETS_TEXT_BUCHAREST, reason, f"the run's reason should name the reset time:\n{state}")
        again = self.p.hook("stop", {"session_id": "s1", "stop_hook_active": True}) or {}
        self.assertNotEqual(again.get("decision"), "block", again)


if __name__ == "__main__":
    unittest.main()
