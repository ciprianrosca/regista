"""Unit and hook-level tests for the usage records: agent_tokens, current_step, append_usage_record, and the
records the subagent-start, subagent-stop and commit hooks append to .regista/usage.jsonl (the end-to-end check is
in tests/e2e/test_usage_record.py).

Every hook runs as a subprocess, the way Claude Code runs it, against the local fake usage server in
tests/fake_usage.py, with a temporary home folder (HOME), state folder (XDG_STATE_HOME), cache folder
(XDG_CACHE_HOME) and credentials file holding a fake token. Nothing here reaches the network, the real home folder
or the real ~/.claude/.credentials.json.

Run: python3 -m unittest discover -s tests"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from unittest import mock

TEST_HOME = tempfile.mkdtemp(prefix="regista-urec-home-")
atexit.register(shutil.rmtree, TEST_HOME, True)
os.environ.update(HOME=TEST_HOME, CODEX_HOME=os.path.join(TEST_HOME, ".codex"))

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.abspath(os.path.join(HERE, "..", "scripts", "regista.py"))
sys.path.insert(0, os.path.dirname(TOOL))
sys.path.insert(0, HERE)
import regista  # noqa: E402
from fake_usage import FAKE_TOKEN, FakeUsageServer, closed_port_url, rising_replies, usage_reply, write_credentials  # noqa: E402,E501

PY = sys.executable
NOT_PLAN_VARS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")
USAGE_KEYS = {"state", "session_pct", "week_pct", "session_resets_at", "week_resets_at"}
TOKEN_KEYS = {"input", "output", "cache_write", "cache_read"}
SECRET_PROMPT = "SECRET-PROMPT-TEXT-do-not-store"
SECRET_ANSWER = "SECRET-ANSWER-TEXT-do-not-store"

# Two model calls; msg_1 appears on two lines with the same usage (Claude Code repeats it), so it counts once.
TRANSCRIPT_ROWS = [
    {"type": "user", "message": {"role": "user", "content": SECRET_PROMPT}},
    {"type": "assistant", "message": {"id": "msg_1", "model": "claude-x", "content": [
        {"type": "thinking", "thinking": SECRET_ANSWER}], "usage": {
        "input_tokens": 3, "output_tokens": 50, "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 20000}}},
    {"type": "assistant", "message": {"id": "msg_1", "model": "claude-x", "content": [
        {"type": "text", "text": SECRET_ANSWER}], "usage": {
        "input_tokens": 3, "output_tokens": 50, "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 20000}}},
    {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": SECRET_PROMPT}]}},
    {"type": "assistant", "message": {"id": "msg_2", "model": "claude-y", "content": [
        {"type": "text", "text": SECRET_ANSWER}], "usage": {
        "input_tokens": 2, "output_tokens": 30, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 21000}}},
]
TRANSCRIPT_TOKENS = {"input": 5, "output": 80, "cache_write": 1000, "cache_read": 41000}
TRANSCRIPT_MODEL = "claude-y"


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


def spec(step="stats", milestone="M1", n=2, **extra):
    return {"step": step, "milestone": milestone, "title": step.title(), **extra,
            "requirements": [{"id": f"{step.upper()}-{i + 1}", "text": f"requirement {i + 1}", "verify": "test"}
                             for i in range(n)]}


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                          check=True).stdout


class RecordBase(unittest.TestCase):
    """A git repository set up with `regista.py init`, a fake usage server, a credentials file with a fake token,
    and temporary home, state and cache folders for every hook it runs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="regista-urec-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.state_home = os.path.join(self.tmp, "state")
        self.cache_home = os.path.join(self.tmp, "cache")
        self.creds = write_credentials(os.path.join(self.tmp, "credentials.json"))
        self.srv = FakeUsageServer(reply=usage_reply(37.0, 82.0)).start()
        self.addCleanup(self.srv.stop)
        env = mock.patch.dict(os.environ, {"HOME": self.home, "XDG_STATE_HOME": self.state_home,
                                           "XDG_CACHE_HOME": self.cache_home, "REGISTA_USAGE_URL": self.srv.url,
                                           "REGISTA_CREDENTIALS": self.creds, "TZ": "Europe/Bucharest"})
        env.start()
        self.addCleanup(env.stop)
        for k in NOT_PLAN_VARS + ("REGISTA_USAGE_CACHE_SECONDS", "CLAUDE_PROJECT_DIR"):
            os.environ.pop(k, None)
        self.extra_env = {}
        self.root = self.make_project()

    # --- the project

    def make_project(self):
        root = os.path.join(self.tmp, "project")
        os.makedirs(root)
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "t@example.com")
        git(root, "config", "user.name", "T")
        git(root, "config", "commit.gpgsign", "false")
        subprocess.run([PY, TOOL, "init", "--name", "Demo"], cwd=root, env=self.env(), check=True,
                       capture_output=True)
        self.root = root
        self.edit_config(gates=[{"name": "tests", "run": "echo ok", "when": ["commit"]}], protected=[],
                         review={"required_for": []},
                         requirements={"enforce_coverage": False, "milestone_needs_e2e": False})
        self.write("README.md", "demo\n")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "init")
        return root

    def edit_config(self, **changes):
        path = os.path.join(self.root, ".regista", "config.json")
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.update(changes)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)

    def write(self, rel, text):
        fp = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)
        return fp

    def commit_all(self, msg="work"):
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", msg)

    def add_spec(self, name="stats", commit=True, **kw):
        fp = self.write(f".regista/specs/{name}.json", json.dumps(spec(**kw)))
        if commit:
            self.commit_all("spec")
        return fp

    # --- running hooks

    def env(self, **extra):
        drop = ("REGISTA_", "DAEDALUS_", "GIT_")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(drop) and not k.endswith("_PROJECT_DIR") and k not in NOT_PLAN_VARS}
        env.update(HOME=self.home, CODEX_HOME=os.path.join(self.home, ".codex"), XDG_STATE_HOME=self.state_home,
                   XDG_CACHE_HOME=self.cache_home, REGISTA_USAGE_URL=self.srv.url, REGISTA_CREDENTIALS=self.creds,
                   TZ="Europe/Bucharest", PYTHONDONTWRITEBYTECODE="1")
        env.update(self.extra_env)
        env.update(extra)
        return env

    def hook(self, event, payload, **extra_env):
        data = {"cwd": self.root, **payload}
        r = subprocess.run([PY, TOOL, "hook", event], input=json.dumps(data), cwd=self.root, capture_output=True,
                           text=True, encoding="utf-8", env=self.env(**extra_env), timeout=120)
        self.last = r
        self.assertNotIn("Traceback", r.stdout + r.stderr, f"hook {event} crashed:\n{r.stderr}")
        return json.loads(r.stdout) if r.stdout.strip() else None

    def start(self, agent_id="b1", agent_type="regista:builder", session="s1", **extra):
        return self.hook("subagent-start", {"session_id": session, "agent_id": agent_id, "agent_type": agent_type,
                                            "hook_event_name": "SubagentStart", **extra})

    def stop(self, agent_id="b1", agent_type="regista:builder", session="s1", **extra):
        return self.hook("subagent-stop", {"session_id": session, "agent_id": agent_id, "agent_type": agent_type,
                                           "hook_event_name": "SubagentStop", "stop_hook_active": False, **extra})

    def commit_hook(self, cmd='git commit -m "feat: work"', session="s1"):
        return self.hook("pre-tool", {"session_id": session, "tool_name": "Bash", "tool_input": {"command": cmd},
                                      "hook_event_name": "PreToolUse"})

    # --- reading what was written

    def usage_file(self):
        return regista.harness_path(self.root, "usage.jsonl")

    def records(self, kind=None):
        path = self.usage_file()
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(ln) for ln in f if ln.strip()]
        return [r for r in rows if kind is None or r.get("kind") == kind]

    def projects_list(self, state_home=None):
        path = os.path.join(state_home or self.state_home, "regista", "usage-projects.json")
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def assert_common(self, rec, kind):
        self.assertEqual(rec["kind"], kind)
        at = datetime.fromisoformat(rec["at"])
        self.assertIsNotNone(at.utcoffset(), f"'at' should carry the local offset: {rec['at']}")
        self.assertIsInstance(rec["milestone"], str)
        self.assertIsInstance(rec["step"], str)
        self.assertIsInstance(rec["other_sessions"], int)
        self.assertEqual(set(rec["usage"]), USAGE_KEYS)

    def subagent_transcript(self, agent_id="b1", session="s1", rows=TRANSCRIPT_ROWS):
        """The host session's transcript and the subagent's own one, laid out as Claude Code lays them out."""
        folder = os.path.join(self.tmp, "transcripts", "-project")
        main = write_jsonl(os.path.join(folder, f"{session}.jsonl"),
                           [{"type": "user", "message": {"role": "user", "content": "build it"}}])
        sub = write_jsonl(os.path.join(folder, session, "subagents", f"agent-{agent_id}.jsonl"), rows)
        return main, sub


# --- UREC-3: agent_tokens


class AgentTokensTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="regista-urec-tok-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_sums_usage_counting_each_message_once_and_returns_last_model(self):  # covers: UREC-3
        path = write_jsonl(os.path.join(self.tmp, "agent.jsonl"), TRANSCRIPT_ROWS)
        tokens, model = regista.agent_tokens(path)
        self.assertEqual(tokens, TRANSCRIPT_TOKENS)
        self.assertEqual(model, TRANSCRIPT_MODEL)
        for v in tokens.values():
            self.assertIsInstance(v, int)

    def test_missing_fields_count_as_zero_and_user_lines_are_ignored(self):  # covers: UREC-3
        rows = [{"type": "user", "message": {"role": "user", "content": "hi",
                                              "usage": {"input_tokens": 999, "output_tokens": 999}}},
                {"type": "assistant", "message": {"id": "m1", "model": "claude-z", "usage": {"output_tokens": 7}}}]
        tokens, model = regista.agent_tokens(write_jsonl(os.path.join(self.tmp, "a.jsonl"), rows))
        self.assertEqual(tokens, {"input": 0, "output": 7, "cache_write": 0, "cache_read": 0})
        self.assertEqual(model, "claude-z")

    def test_missing_transcript_gives_none(self):  # covers: UREC-3
        self.assertEqual(regista.agent_tokens(os.path.join(self.tmp, "nope.jsonl")), (None, ""))
        self.assertEqual(regista.agent_tokens(None), (None, ""))
        self.assertEqual(regista.agent_tokens(""), (None, ""))

    def test_unreadable_transcript_gives_none(self):  # covers: UREC-3
        self.assertEqual(regista.agent_tokens(self.tmp), (None, ""))  # a folder, not a file
        if not (hasattr(os, "geteuid") and os.geteuid() == 0):
            path = write_jsonl(os.path.join(self.tmp, "locked.jsonl"), TRANSCRIPT_ROWS)
            os.chmod(path, 0)
            try:
                self.assertEqual(regista.agent_tokens(path), (None, ""))
            finally:
                os.chmod(path, 0o600)


# --- UREC-5: current_step


class CurrentStepTest(RecordBase):
    def age(self, path, seconds):
        t = time.time() - seconds
        os.utime(path, (t, t))

    def test_no_specs_gives_empty(self):  # covers: UREC-5
        self.assertEqual(regista.current_step(self.root), ("", ""))

    def test_only_planned_specs_gives_empty(self):  # covers: UREC-5
        self.add_spec("later", step="later", milestone="M2", state="planned")
        self.assertEqual(regista.current_step(self.root), ("", ""))

    def test_most_recently_modified_non_planned_spec(self):  # covers: UREC-5
        old = self.add_spec("old", step="old", milestone="M1", commit=False)
        new = self.add_spec("new", step="new", milestone="M2", commit=False)
        planned = self.add_spec("planned", step="planned", milestone="M3", state="planned", commit=False)
        self.commit_all("specs")
        self.age(old, 300)
        self.age(new, 100)
        self.age(planned, 10)  # newest, but planned: skipped
        self.assertEqual(regista.current_step(self.root), ("M2", "new"))

    def test_step_falls_back_to_file_name(self):  # covers: UREC-5
        fp = self.write(".regista/specs/pages.json", json.dumps({"milestone": "M4", "title": "Pages",
                                                                 "requirements": []}))
        self.commit_all("spec")
        self.age(fp, 5)
        self.assertEqual(regista.current_step(self.root), ("M4", "pages"))

    def test_the_one_spec_in_the_change_set_wins(self):  # covers: UREC-5
        mine = self.add_spec("mine", step="mine", milestone="M1")
        other = self.add_spec("other", step="other", milestone="M2")
        # change only `mine` (uncommitted), but make `other` the most recently modified file
        self.write(".regista/specs/mine.json", json.dumps(spec(step="mine", milestone="M1", n=3)))
        self.age(mine, 600)
        self.age(other, 1)
        self.assertEqual(regista.current_step(self.root), ("M1", "mine"))

    def test_a_planned_spec_in_the_change_set_doesnt_count(self):  # covers: UREC-5
        self.add_spec("active", step="active", milestone="M1")
        later = self.add_spec("later", step="later", milestone="M2", state="planned", commit=False)
        self.age(later, 1)
        self.assertEqual(regista.current_step(self.root), ("M1", "active"))

    def test_two_specs_in_the_change_set_fall_back_to_newest(self):  # covers: UREC-5
        a = self.add_spec("a", step="a", milestone="M1", commit=False)
        b = self.add_spec("b", step="b", milestone="M2", commit=False)
        self.age(a, 200)
        self.age(b, 20)
        self.assertEqual(regista.current_step(self.root), ("M2", "b"))


# --- UREC-1, UREC-2: the agent records


class AgentRecordsTest(RecordBase):
    def test_subagent_start_appends_agent_start_record(self):  # covers: UREC-1
        self.add_spec("stats", step="stats", milestone="M1")
        out = self.start("b1", "regista:builder", session="host-1")
        self.assertIn("builder", out["hookSpecificOutput"]["additionalContext"])  # the usual answer still comes
        recs = self.records()
        self.assertEqual(len(recs), 1, recs)
        rec = recs[0]
        self.assert_common(rec, "agent_start")
        self.assertEqual(rec["agent_id"], "b1")
        self.assertEqual(rec["role"], "builder")
        self.assertEqual(rec["agent_type"], "regista:builder")
        self.assertEqual((rec["milestone"], rec["step"]), ("M1", "stats"))
        self.assertEqual(rec["session_id"], "host-1")
        self.assertEqual(rec["usage"]["state"], "ok")
        self.assertEqual(rec["usage"]["session_pct"], 37.0)
        self.assertEqual(rec["usage"]["week_pct"], 82.0)
        self.assertTrue(rec["usage"]["week_resets_at"])
        self.assertTrue(rec["usage"]["session_resets_at"])
        self.assertNotIn("tokens", rec)

    def test_roles_follow_agent_type(self):  # covers: UREC-1
        self.start("t1", "regista:test-writer")
        self.start("x1", "Explore")
        roles = {r["agent_id"]: (r["role"], r["agent_type"]) for r in self.records("agent_start")}
        self.assertEqual(roles, {"t1": ("test-writer", "regista:test-writer"), "x1": ("other", "Explore")})

    def test_subagent_stop_appends_tokens_seconds_and_usage(self):  # covers: UREC-2
        self.add_spec("stats", step="stats", milestone="M1")
        main, sub = self.subagent_transcript("b1")
        self.start("b1", transcript_path=main)
        time.sleep(1.2)
        self.stop("b1", transcript_path=main, agent_transcript_path=sub)
        stops = self.records("agent_stop")
        self.assertEqual(len(stops), 1, self.records())
        rec = stops[0]
        self.assert_common(rec, "agent_stop")
        self.assertEqual((rec["agent_id"], rec["role"], rec["agent_type"]), ("b1", "builder", "regista:builder"))
        self.assertEqual((rec["milestone"], rec["step"]), ("M1", "stats"))
        self.assertEqual(rec["tokens"], TRANSCRIPT_TOKENS)
        self.assertEqual(rec["model"], TRANSCRIPT_MODEL)
        self.assertIsInstance(rec["seconds"], float)
        self.assertGreaterEqual(rec["seconds"], 1.0)
        self.assertLess(rec["seconds"], 60.0)
        self.assertEqual(rec["usage"]["state"], "ok")
        self.assertEqual(rec["usage"]["week_pct"], 82.0)

    def test_subagent_transcript_found_from_host_transcript(self):  # covers: UREC-2
        # without agent_transcript_path, the subagent's transcript sits next to the host's, as Claude Code keeps it
        main, _ = self.subagent_transcript("b2")
        self.start("b2", transcript_path=main)
        self.stop("b2", transcript_path=main)
        rec = self.records("agent_stop")[0]
        self.assertEqual(rec["tokens"], TRANSCRIPT_TOKENS)
        self.assertEqual(rec["model"], TRANSCRIPT_MODEL)

    def test_stop_without_recorded_start_or_transcript(self):  # covers: UREC-2
        self.stop("ghost", agent_transcript_path=os.path.join(self.tmp, "missing.jsonl"))
        rec = self.records("agent_stop")[0]
        self.assertIsNone(rec["seconds"])
        self.assertIsNone(rec["tokens"])
        self.assertEqual(rec["model"], "")


# --- UREC-4: the step record


class StepRecordTest(RecordBase):
    def setUp(self):
        super().setUp()
        self.write("src/a.py", "a = 1\nb = 2\nc = 3\n")
        self.add_spec("stats", step="stats", milestone="M1", n=3)  # commits src/a.py too

    def stage_change(self):
        self.write("src/a.py", "a = 1\nb = 20\nc = 3\nd = 4\ne = 5\n")  # +3 -1
        self.write("tests/test_a.py", "# covers: STATS-1\nx = 1\n")  # +2
        self.write(".regista/status.md", "one\ntwo\nthree\nfour\nfive\n")  # a harness record: not counted
        git(self.root, "add", "-A")

    def test_passing_commit_appends_step_record_with_fresh_usage(self):  # covers: UREC-4
        self.srv.configure(replies=rising_replies(5, week_start=40.0, week_step=0.5))
        self.start("b1")  # reads once: 40.0, cached for 60 seconds
        self.stage_change()
        before = self.srv.count
        out = self.commit_hook()
        self.assertNotEqual((out or {}).get("hookSpecificOutput", {}).get("permissionDecision"), "deny", out)
        self.assertGreater(self.srv.count, before, "the step record needs a fresh reading, not the cached one")
        steps = self.records("step")
        self.assertEqual(len(steps), 1, self.records())
        rec = steps[0]
        self.assert_common(rec, "step")
        self.assertEqual((rec["milestone"], rec["step"]), ("M1", "stats"))
        self.assertEqual(rec["commit_files"], 2)
        self.assertEqual(rec["lines_changed"], 6)
        self.assertEqual(rec["requirements"], 3)
        self.assertEqual(rec["session_id"], "s1")
        self.assertEqual(rec["usage"]["state"], "ok")
        start = self.records("agent_start")[0]
        self.assertGreater(rec["usage"]["week_pct"], start["usage"]["week_pct"])

    def test_refused_commit_appends_no_step_record(self):  # covers: UREC-4
        self.edit_config(gates=[{"name": "tests", "run": "exit 1", "when": ["commit"]}])
        self.commit_all("a failing gate")
        self.stage_change()
        out = self.commit_hook()
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.records("step"), [])


# --- UREC-6: other sessions


class OtherSessionsTest(RecordBase):
    def touch(self, rel, age=0.0, locked=False):
        fp = os.path.join(self.home, ".claude", "projects", *rel.split("/"))
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write('{"type": "user"}\n')
        t = time.time() - age
        os.utime(fp, (t, t))
        if locked:
            os.chmod(fp, 0)  # only names and times are read, never contents
            self.addCleanup(os.chmod, fp, 0o600)
        return fp

    def test_counts_recent_session_transcripts_other_than_own(self):  # covers: UREC-6
        lock = not (hasattr(os, "geteuid") and os.geteuid() == 0)
        own = self.touch("-proj-a/s1.jsonl")
        self.touch("-proj-a/s2.jsonl", age=60, locked=lock)              # counts
        self.touch("-proj-b/s3.jsonl", age=9 * 60, locked=lock)          # counts
        self.touch("-proj-b/s4.jsonl", age=11 * 60)                      # too old
        self.touch("-proj-a/s2/subagents/agent-x.jsonl")                 # a subagent, not a session
        self.touch("-proj-a/notes.txt")                                  # not a transcript
        self.touch("stray.jsonl")                                        # not inside a project folder
        self.touch("-proj-c/deeper/s5.jsonl")                            # not directly in a project folder
        self.start("b1", session="s1", transcript_path=own)
        self.assertEqual(self.records("agent_start")[0]["other_sessions"], 2)

    def test_no_claude_folder_gives_zero(self):  # covers: UREC-6
        self.start("b1")
        self.assertEqual(self.records("agent_start")[0]["other_sessions"], 0)


# --- UREC-7: switched off


class DisabledTest(RecordBase):
    def test_disabled_meter_writes_nothing_and_hooks_answer_as_before(self):  # covers: UREC-7
        self.edit_config(usage={"enabled": False})
        self.commit_all("meter off")
        main, sub = self.subagent_transcript("b1")
        out = self.start("b1", transcript_path=main)
        self.assertIn("builder", out["hookSpecificOutput"]["additionalContext"])
        self.stop("b1", transcript_path=main, agent_transcript_path=sub)
        self.write("src/b.py", "b = 1\n")
        git(self.root, "add", "-A")
        out = self.commit_hook()
        self.assertIsNone((out or {}).get("hookSpecificOutput", {}).get("permissionDecision"), out)
        self.assertFalse(os.path.exists(self.usage_file()), "no usage record when the meter is off")
        self.assertIsNone(self.projects_list())


# --- UREC-8: never in the way


class NeverBlocksTest(RecordBase):
    def test_unwritable_usage_file_still_gives_the_usual_answers(self):  # covers: UREC-8
        os.makedirs(self.usage_file())  # a folder where the file should be: every append fails
        main, sub = self.subagent_transcript("b1")
        out = self.start("b1", transcript_path=main)
        self.assertEqual(self.last.returncode, 0, self.last.stderr)
        self.assertIn("builder", out["hookSpecificOutput"]["additionalContext"])
        self.stop("b1", transcript_path=main, agent_transcript_path=sub)
        self.assertEqual(self.last.returncode, 0, self.last.stderr)
        self.write("src/b.py", "b = 1\n")
        git(self.root, "add", "-A")
        out = self.commit_hook()
        self.assertEqual(self.last.returncode, 0, self.last.stderr)
        self.assertIsNone((out or {}).get("hookSpecificOutput", {}).get("permissionDecision"), out)

    def test_unknown_usage_still_writes_records(self):  # covers: UREC-8
        self.extra_env = {"REGISTA_USAGE_URL": closed_port_url()}
        self.start("b1", transcript_path=os.path.join(self.tmp, "no-such-host.jsonl"))
        self.stop("b1", agent_transcript_path=os.path.join(self.tmp, "no-such-agent.jsonl"))
        self.write("src/b.py", "b = 1\n")
        git(self.root, "add", "-A")
        out = self.commit_hook()
        self.assertIsNone((out or {}).get("hookSpecificOutput", {}).get("permissionDecision"), out)
        kinds = [r["kind"] for r in self.records()]
        self.assertEqual(kinds, ["agent_start", "agent_stop", "step"])
        for rec in self.records():
            self.assertEqual(rec["usage"]["state"], "unknown")
            self.assertIsNone(rec["usage"]["session_pct"])
            self.assertIsNone(rec["usage"]["week_pct"])
        self.assertIsNone(self.records("agent_stop")[0]["tokens"])

    def test_bad_transcript_still_writes_the_stop_record(self):  # covers: UREC-8
        bad = os.path.join(self.tmp, "bad.jsonl")
        with open(bad, "wb") as f:
            f.write(b"\xff\xfe not json at all\n{\"type\": \"assistant\", \"message\": 7}\n")
        self.start("b1")
        self.stop("b1", agent_transcript_path=bad)
        self.assertEqual(self.last.returncode, 0, self.last.stderr)
        self.assertEqual(len(self.records("agent_stop")), 1)

    def test_commit_outside_git_history_still_answers(self):  # covers: UREC-8
        # a repository with no commit yet: git diff against HEAD fails, the hook still answers
        shutil.rmtree(os.path.join(self.root, ".git"))
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "t@example.com")
        git(self.root, "config", "user.name", "T")
        git(self.root, "add", "-A")
        self.commit_hook()
        self.assertEqual(self.last.returncode, 0, self.last.stderr)

    def test_append_usage_record_never_raises(self):  # covers: UREC-8
        cfg = regista.load_config(self.root)
        os.makedirs(self.usage_file())
        regista.append_usage_record(self.root, cfg, {"kind": "step", "milestone": "", "step": ""})
        regista.append_usage_record(os.path.join(self.tmp, "no-such-project"), {}, {"kind": "step"})
        regista.append_usage_record(self.root, cfg, None)


# --- UREC-9: the machine-wide list of projects


class ProjectsListTest(RecordBase):
    def test_root_added_once(self):  # covers: UREC-9
        other = os.path.join(self.tmp, "elsewhere")
        os.makedirs(os.path.join(self.state_home, "regista"))
        with open(os.path.join(self.state_home, "regista", "usage-projects.json"), "w", encoding="utf-8") as f:
            json.dump([other], f)
        self.start("b1")
        self.stop("b1")
        self.start("b2")
        listed = self.projects_list()
        self.assertIsInstance(listed, list)
        self.assertIn(other, listed, "existing entries stay")
        mine = [p for p in listed if os.path.realpath(p) == os.path.realpath(self.root)]
        self.assertEqual(len(mine), 1, listed)
        self.assertTrue(os.path.isabs(mine[0]))

    def test_default_location_without_xdg_state_home(self):  # covers: UREC-9
        env = self.env()
        env.pop("XDG_STATE_HOME")
        r = subprocess.run([PY, TOOL, "hook", "subagent-start"], cwd=self.root, capture_output=True, text=True,
                           encoding="utf-8", env=env, timeout=120,
                           input=json.dumps({"cwd": self.root, "session_id": "s1", "agent_id": "b1",
                                             "agent_type": "regista:builder"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        listed = self.projects_list(state_home=os.path.join(self.home, ".local", "state"))
        self.assertEqual([os.path.realpath(p) for p in listed or []], [os.path.realpath(self.root)])


# --- UREC-11: nothing secret stored


class NoSecretsTest(RecordBase):
    def test_records_hold_no_token_and_no_transcript_text(self):  # covers: UREC-11
        self.srv.configure(reply=usage_reply(37.0, 82.0), echo_token=False)
        main, sub = self.subagent_transcript("b1")
        self.start("b1", transcript_path=main)
        self.stop("b1", transcript_path=main, agent_transcript_path=sub)
        self.write("src/b.py", "b = 1\n")
        git(self.root, "add", "-A")
        self.commit_hook()
        self.assertEqual([r["kind"] for r in self.records()], ["agent_start", "agent_stop", "step"])
        with open(self.usage_file(), encoding="utf-8") as f:
            text = f.read()
        with open(os.path.join(self.state_home, "regista", "usage-projects.json"), encoding="utf-8") as f:
            text += f.read()
        for secret in (FAKE_TOKEN, "sk-ant-", SECRET_PROMPT, SECRET_ANSWER, "build it", "thinking"):
            self.assertNotIn(secret, text)
        self.assertIn(TRANSCRIPT_MODEL, text)


if __name__ == "__main__":
    unittest.main()
