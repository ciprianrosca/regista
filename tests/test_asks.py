"""Questions that wait on the user, the overseer's news, and the user's one-line commands (lessons 6, 11 and 12 of
the first parallel builds, 26 to 28 October 2026).

- A worker waited 44 minutes, twice, for an OK nobody saw; the first change only added a gate.
- Session start brought in 82 events about workers removed a day earlier, picked the wrong latest handoff after a
  folder move, and asked for milestone codes and dated gate results.
- A plain "ok" or "1. All ok" to a question that named the checks wasn't recorded; sessions were opened by hand.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_asks -v"""
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# test_regista points HOME and CODEX_HOME at a temporary folder when it is imported.
from test_regista import (FAKE, FLEET_PLAN, PY, TOOL, TRY_SPEC, Project, as_agent, bash, clean_env,  # noqa: E402
                          decision, jload, jsave, person_environ, queued, regista, sh)
from test_migration import OldProject, run_tool, save  # noqa: E402


def now_utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def said(path, who, text, at=None):
    """Append one main-thread message to a Claude Code transcript: the assistant's words, or a person's prompt."""
    if who == "assistant":
        d = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
    else:
        d = {"type": "user", "message": {"role": "user", "content": text}}
    d["timestamp"] = at or now_utc()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(d) + "\n")


def reason(out):
    return (out or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason", "")


def context(out):
    return (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")


# --- lesson 6: ask only when a config change loosens something --------------------------------------------------

class ConfigEdits(unittest.TestCase):
    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}], protected=["data/truth/**"])
        self.addCleanup(self.p.cleanup)
        self.cfg = os.path.join(self.p.root, ".regista", "config.json")
        with open(self.cfg, encoding="utf-8") as f:
            self.text = f.read()

    def edit(self, old, new, role=None):
        return self.p.hook("pre-tool", as_agent(role, "Edit", file_path=self.cfg, old_string=old, new_string=new))

    def test_adding_a_gate_or_a_protected_path_goes_through(self):
        self.assertIsNone(decision(self.edit('"run": "echo ok"}]', '"run": "echo ok"}, {"name": "lint", "run": "true"}]')))
        self.assertIsNone(decision(self.edit('["data/truth/**"]', '["data/truth/**", "labels/**"]')))
        c = json.loads(self.text)
        c["gates"].append({"name": "e2e", "run": "true", "evidence": "e2e"})
        out = self.p.hook("pre-tool", as_agent(None, "Write", file_path=self.cfg, content=json.dumps(c, indent=2)))
        self.assertIsNone(decision(out))

    def test_a_loosening_edit_still_asks_and_says_what_it_loosens(self):
        out = self.edit('{"name": "tests", "run": "echo ok"}', '{"name": "tests", "run": "true"}')
        self.assertEqual(decision(out), "ask")
        self.assertIn("the gate 'tests' was changed", reason(out))
        out = self.edit('["data/truth/**"]', "[]")
        self.assertEqual(decision(out), "ask")
        self.assertIn("data/truth/**", reason(out))

    def test_other_settings_still_ask(self):
        c = json.loads(self.text)
        c["gates"].append({"name": "lint", "run": "true"})
        for key, value in (("context", {"limit_pct": 95}), ("fleet", {"claude_args": ["--dangerously-skip-permissions"]}),
                           ("review", {"ignore": ["src/**"]})):
            out = self.p.hook("pre-tool", as_agent(None, "Write", file_path=self.cfg, content=json.dumps({**c, key: value})))
            self.assertEqual(decision(out), "ask", key)

    def test_a_new_gate_over_the_source_is_a_loosening(self):
        out = self.edit('"run": "echo ok"}]', '"run": "echo ok"}, {"name": "x", "run": "true", "tests": ["src/**"]}]')
        self.assertEqual(decision(out), "ask")
        self.assertIn("counts source files as its tests", reason(out))

    def test_what_cant_be_read_in_advance_still_asks(self):
        self.assertEqual(decision(self.edit("no such text", "x")), "ask")
        self.assertEqual(decision(self.p.hook("pre-tool", as_agent(None, "Write", file_path=self.cfg, content="{oops"))),
                         "ask")
        self.assertEqual(decision(self.p.hook("pre-tool", as_agent(None, "Bash", command=(
            "python3 -c \"import json;p='.regista/config.json';c=json.load(open(p));"
            "c['gates'].append({'name':'x','run':'true'});json.dump(c,open(p,'w'))\"")))), "ask")
        self.assertEqual(decision(self.edit('["data/truth/**"]', '["data/truth/**", "x/**"]', role="builder")), "deny")

    def test_the_users_approvals_are_written_only_by_the_harness(self):
        for payload in (as_agent(None, "Bash", command='echo \'{"granted": {}}\' > .regista/approvals.json'),
                        as_agent(None, "Write", file_path=os.path.join(self.p.root, ".regista", "approvals.json"),
                                 content="{}")):
            self.assertEqual(decision(self.p.hook("pre-tool", payload)), "deny", payload)


# --- lesson 6: a worker's question reaches the overseer, and the user's yes comes back through it ----------------

FAKE_ENV_TOOLS = ("claude", "tmux")


class FleetCase(unittest.TestCase):
    """A project with the fake `claude` and `tmux` of the fleet tests."""

    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "true", "when": ["commit"]}],
                         review={"required_for": []}, requirements={"enforce_coverage": False,
                                                                     "milestone_needs_e2e": False})
        self.p.commit(".regista/plan.md", FLEET_PLAN, "plan")
        self.fake = tempfile.mkdtemp(prefix="regista-fake-")
        for tool in FAKE_ENV_TOOLS:
            path = os.path.join(self.fake, tool)
            with open(path, "w", encoding="utf-8") as f:
                f.write(FAKE.replace("PYTHON", sys.executable))
            os.chmod(path, 0o755)
        self.env = {"REGISTA_CLAUDE": os.path.join(self.fake, "claude"), "REGISTA_TMUX": os.path.join(self.fake, "tmux"),
                    "FAKE_DIR": self.fake, "REGISTA_LAUNCH_WAIT": "3", "CLAUDE_CODE_SESSION_ID": "overseer-1"}
        self.t = os.path.join(self.fake, "overseer.jsonl")
        open(self.t, "w").close()

    def tearDown(self):
        sh(self.p.root, "git", "worktree", "prune")
        self.p.cleanup()
        shutil.rmtree(self.fake, ignore_errors=True)

    def fleet(self, *args):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        env.update(self.env)
        return subprocess.run([PY, TOOL, "fleet", *args], cwd=self.p.root, capture_output=True, text=True,
                              encoding="utf-8", env=env)

    def hook(self, event, payload, cwd=None):
        return self.p.hook(event, {"cwd": cwd or self.p.root, **payload}, extra_env=self.env)

    def wt(self, name="m2"):
        return os.path.join(self.p.root, ".regista", "worktrees", name)

    def spawn(self, target="M2"):
        self.hook("pre-tool", {**bash(f'python3 "{TOOL}" fleet spawn {target}'), "session_id": "overseer-1",
                               "permission_mode": "acceptEdits"})
        r = self.fleet("spawn", target)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


LOOSEN = "sed -i 's/true/false/' .regista/config.json"


class WorkerAsks(FleetCase):
    def worker_tries(self, cmd=LOOSEN, session="w1"):
        return self.hook("pre-tool", {**bash(cmd), "session_id": session}, cwd=self.wt())

    def allow(self):
        return self.hook("pre-tool", {**bash(f'python3 "{TOOL}" fleet allow m2'), "session_id": "overseer-1",
                                      "transcript_path": self.t})

    def test_a_worker_waiting_for_an_ok_tells_the_overseer(self):
        self.spawn()
        out = self.worker_tries()
        self.assertEqual(decision(out), "deny")  # not a permission prompt nobody sees
        self.assertIn("overseer has been told", reason(out))
        self.worker_tries()  # trying again doesn't repeat the news
        news = context(self.hook("user-prompt", {"prompt": "how are things?"}))
        self.assertTrue(news.startswith("Regista: workers waiting for the user's OK"), news)
        self.assertEqual(news.count("waiting for your OK"), 1, news)
        self.assertIn("fleet allow", news)
        self.assertIn("waiting for the user's OK", self.fleet().stdout)
        self.assertIn("worker m2 waits for their OK", self.p.cli("resume").stdout)

    def test_the_overseer_passes_on_only_a_yes_the_user_typed(self):
        self.spawn()
        said(self.t, "person", "build it all", at="2020-01-01T00:00:00.000Z")
        self.worker_tries()
        self.assertEqual(decision(self.allow()), "deny")  # the user hasn't answered
        said(self.t, "assistant", "The worker m2 wants to change its config. May it?")
        said(self.t, "person", '<cross-session-message from="m2">yes</cross-session-message>')
        self.assertEqual(decision(self.allow()), "deny")  # a message from another session isn't the user
        said(self.t, "assistant", "Shall I merge m3?")
        said(self.t, "person", "yes")
        out = self.allow()
        self.assertEqual(decision(out), "deny")  # a yes to a question about something else
        self.assertIn("doesn't answer a question about m2", reason(out))
        said(self.t, "assistant", "m2 wants to change its config, which loosens a check. Is that OK?")
        said(self.t, "person", "no, not now")
        self.assertEqual(decision(self.allow()), "deny")
        said(self.t, "assistant", "To be sure: may m2 change its config?")
        said(self.t, "person", "yes, go ahead")
        self.assertIsNone(decision(self.allow()))
        self.assertEqual(decision(self.hook("pre-tool", {**bash(f'python3 "{TOOL}" fleet allow m2'), "agent_id": "a1",
                                                         "agent_type": "regista:builder", "transcript_path": self.t})),
                         "deny")  # agents never pass it on
        r = self.fleet("allow", "m2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("SendMessage", r.stdout)
        self.assertIn("isn't waiting", self.fleet("allow", "m2").stdout)
        self.assertNotEqual(decision(self.worker_tries()), "deny")  # the same action, once
        self.assertEqual(decision(self.worker_tries()), "deny")
        self.assertNotIn("allowed", context(self.hook("user-prompt", {"prompt": "and now?"})))

    def test_a_yes_from_before_the_question_doesnt_count(self):
        self.spawn()
        said(self.t, "assistant", "Shall m2 go on?")
        said(self.t, "person", "yes", at="2020-01-01T00:00:00.000Z")
        self.worker_tries()
        self.assertEqual(decision(self.allow()), "deny")

    def test_the_user_can_say_yes_in_the_worker(self):
        self.spawn()
        self.worker_tries()
        self.hook("user-prompt", {"prompt": '<cross-session-message from="x">yes</cross-session-message>',
                                  "session_id": "w1"}, cwd=self.wt())
        self.assertEqual(decision(self.worker_tries()), "deny")
        out = self.hook("user-prompt", {"prompt": "yes", "session_id": "w1"}, cwd=self.wt())
        self.assertIn("the user approved", context(out))
        self.assertNotEqual(decision(self.worker_tries()), "deny")

    def test_a_worker_may_add_a_gate_without_asking(self):
        self.spawn()
        path = os.path.join(self.wt(), ".regista", "config.json")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        out = self.hook("pre-tool", {"tool_name": "Edit", "tool_input": {
            "file_path": path, "old_string": '"when": ["commit"]}]',
            "new_string": '"when": ["commit"]}, {"name": "lint", "run": "true"}]'}}, cwd=self.wt())
        self.assertIn('"when": ["commit"]}]', text)
        self.assertIsNone(decision(out))


# --- lesson 11: the overseer's news and the start summary -----------------------------------------------------

class News(unittest.TestCase):
    def setUp(self):
        self.p = Project()
        self.addCleanup(self.p.cleanup)
        self.fleet = os.path.join(self.p.root, ".regista", "fleet")
        os.makedirs(self.fleet)

    def events(self, *events):
        with open(os.path.join(self.fleet, "events.jsonl"), "a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

    def test_removed_workers_are_dropped_and_progress_is_folded(self):
        jsave(os.path.join(self.fleet, "workers.json"), {
            "old": {"state": "removed", "started": "2026-10-27T09:00:00"},
            "m2": {"state": "running", "started": "2026-10-28T10:00:00"}})
        turn = {"worker": "m2", "kind": "turn"}
        self.events({"at": "2026-10-27T12:00:00", "worker": "old", "kind": "turn", "text": "Built the old page."},
                    {"at": "2026-10-27T12:00:00", "worker": "old", "kind": "user", "text": "make it red"},
                    {**turn, "at": "2026-10-28T09:00:00", "text": "From an earlier worker called m2."},
                    {**turn, "at": "2026-10-28T10:05:00", "text": "Step 1 done, on to step 2.", "commits": ["a", "b"]},
                    {**turn, "at": "2026-10-28T10:20:00", "text": "The builder is on step 2."},
                    {**turn, "at": "2026-10-28T10:40:00", "text": "Step 3 of 4 committed.", "commits": ["c"]},
                    {**turn, "at": "2026-10-28T11:00:00", "text": "M2 is ready to merge."},
                    {"at": "2026-10-28T11:05:00", "worker": "m2", "kind": "ask", "text": "the gate 'e2e' was removed"},
                    {"at": "2026-10-28T11:06:00", "worker": "m2", "kind": "user", "text": "use the blue"})
        news = regista.fleet_news(self.p.root)
        self.assertNotIn("old", news.replace("older", ""))
        self.assertNotIn("earlier worker", news)
        self.assertIn("3 progress update(s) from 10:05 to 10:40, 3 commit(s)", news)
        self.assertIn('the latest: "Step 3 of 4 committed."', news)
        self.assertNotIn("Step 1 done", news)
        self.assertIn('paused, saying: "M2 is ready to merge."', news)
        self.assertIn('the user wrote to it directly: "use the blue"', news)
        self.assertLess(news.index("waiting for your OK: the gate 'e2e' was removed"), news.index("news from the workers"))
        self.assertEqual(regista.fleet_news(self.p.root), "")  # once each

    def test_news_of_only_removed_workers_is_nothing(self):
        jsave(os.path.join(self.fleet, "workers.json"), {"old": {"state": "removed", "started": "2026-10-27T09:00:00"}})
        self.events(*[{"at": "2026-10-27T12:00:00", "worker": "old", "kind": "turn", "text": f"step {k}"}
                      for k in range(82)])
        self.assertEqual(regista.fleet_news(self.p.root), "")


class Handoffs(unittest.TestCase):
    def setUp(self):
        self.p = Project()
        self.addCleanup(self.p.cleanup)

    def handoff(self, name, mtime):
        self.p.write(f".regista/state/{name}", "# h\n## Resume point\nfrom " + name + "\n")
        path = os.path.join(self.p.root, ".regista", "state", name)
        os.utime(path, (mtime, mtime))
        return path

    def test_the_date_in_the_name_comes_first(self):
        moved = time.time()  # a folder move gives every file the same time, or the wrong order
        self.handoff("2026-10-27-later.md", moved - 100)
        self.handoff("2026-10-20-early.md", moved)
        self.handoff("2026-10-27-auto-120000.md", moved - 200)
        cfg = regista.load_config(self.p.root)
        self.assertTrue(regista.newest_handoff(self.p.root, cfg, real_only=True).endswith("2026-10-27-later.md"))
        self.assertTrue(regista.newest_handoff(self.p.root, cfg).endswith("2026-10-27-later.md"))
        self.handoff("2026-10-27-evening.md", moved)  # within a day, the later one wins
        self.assertTrue(regista.newest_handoff(self.p.root, cfg, real_only=True).endswith("2026-10-27-evening.md"))
        self.assertIn("from 2026-10-27-evening.md", self.p.cli("resume").stdout)


class StartSummary(unittest.TestCase):
    def test_session_start_asks_for_plain_words(self):
        p = Project()
        self.addCleanup(p.cleanup)
        p.write(".regista/specs/signup.json", json.dumps(TRY_SPEC))
        ctx = context(p.hook("session-start", {"source": "startup"}))
        self.assertIn("what's done, what's next, and what needs them", ctx)
        self.assertIn("Details", ctx)
        self.assertNotIn("Where we are / Next / Blocked on", ctx)
        self.assertIn("## Waiting on the user\n- try UI-1: Sign-up fits a phone screen", ctx)

    def test_the_start_skill_asks_for_the_same(self):
        with open(os.path.join(os.path.dirname(TOOL), "..", "skills", "start", "SKILL.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("Where we are:", text)
        self.assertIn("**Needs you:**", text)
        self.assertIn("**Details**", text)


# --- lesson 12: the user's checks, sessions, the plugin swap, doctor --------------------------------------------

class ReadyToSend(unittest.TestCase):
    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}])
        self.addCleanup(self.p.cleanup)
        self.p.write(".regista/specs/signup.json", json.dumps(TRY_SPEC))

    def test_try_ends_with_a_line_per_check(self):
        out = self.p.cli("try").stdout
        tail = out[out.index("Tell the harness"):]
        self.assertIn("\n  /regista:verify UI-1 ok\n  /regista:verify UI-3 ok\n", tail)
        self.assertIn("/regista:verify all ok", tail)
        self.assertNotIn("UI-2 ok", tail)  # the gates prove that one

    def test_the_terminal_walkthrough_lists_what_was_skipped(self):
        cfg = regista.load_config(self.p.root)
        buf = io.StringIO()
        # run as in a person's own terminal: without CLAUDE_PROJECT_DIR or an agent's variables in os.environ
        with person_environ(), mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.input", side_effect=["y", "", ""]), redirect_stdout(buf):
            regista.try_ask(self.p.root, cfg)
        self.assertIn("/regista:verify UI-3 ok", buf.getvalue())
        self.assertNotIn("/regista:verify UI-1 ok", buf.getvalue())  # recorded just now

    def test_the_skills_end_questions_with_the_lines(self):
        skills = os.path.join(os.path.dirname(TOOL), "..", "skills")
        for name in ("try", "oversee", "build"):
            with open(os.path.join(skills, name, "SKILL.md"), encoding="utf-8") as f:
                self.assertIn("ready-to-send", f.read(), name)


QUESTION = """UI-1 and UI-3 are ready for you to try.

1. UI-1 — Sign-up fits a phone screen
   Open /signup on your phone.
2. UI-3 — The colours match the brand

Tell the harness what you saw. When a check works, send its line as it is:
  /regista:verify UI-1 ok
  /regista:verify UI-3 ok"""

NUMBERED = """Three things need you:

1. **Phone check, about 5 minutes.** Open /signup on your phone. Then type:
   ```
   /regista:verify UI-1 ok
   ```
2. **Still open:** should a rival own a player anywhere in their squad?
3. **Pushing:** nothing is pushed. Tell me if you want it pushed."""


class OkReplies(unittest.TestCase):
    """A plain "ok" right after the session asked for specific checks records those checks (12 of the lessons)."""

    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}])
        self.addCleanup(self.p.cleanup)
        self.p.write(".regista/specs/signup.json", json.dumps(TRY_SPEC))
        self.t = os.path.join(tempfile.mkdtemp(prefix="regista-tr-"), "s1.jsonl")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.t), True)
        open(self.t, "w").close()

    def state(self):
        return regista.load_json(regista.requirements_path(self.p.root), {})

    def reply(self, prompt, **extra):
        return self.p.hook("user-prompt", {"prompt": prompt, "transcript_path": self.t, **extra})

    def test_ok_records_every_check_the_question_named(self):
        said(self.t, "person", "can I try it?")
        said(self.t, "assistant", QUESTION)
        ctx = context(self.reply("All ok"))
        self.assertIn('the user replied "All ok" to your question that named these checks', ctx)
        self.assertTrue(self.state()["UI-1"]["passes"] and self.state()["UI-3"]["passes"])
        self.assertEqual(self.state()["UI-1"]["by"], "user (replied ok to the checks asked for)")

    def test_one_check_by_its_id_or_by_another_name(self):
        said(self.t, "assistant", QUESTION)
        self.reply("UI-3 ok")
        self.assertEqual(sorted(self.state()), ["UI-3"])
        said(self.t, "assistant", "Read the docs page, then reply `/regista:verify UI-1 ok`.")
        self.reply("Doc ok")
        self.assertTrue(self.state()["UI-1"]["passes"])

    def test_what_isnt_a_plain_yes_to_the_checks_records_nothing(self):
        said(self.t, "assistant", QUESTION)
        for prompt in ("not ok", "mostly ok", "ok, but the logo is cut off", "doc ok", "sounds good",
                       '<cross-session-message from="w">ok</cross-session-message>'):
            self.reply(prompt)
        self.reply("ok", agent_id="a1", agent_type="regista:builder")
        self.assertEqual(self.state(), {})
        said(self.t, "assistant", "M3 is merged. Shall I start M4?")  # the last question named no checks
        self.reply("ok")
        self.assertEqual(self.state(), {})

    def test_only_right_after_the_question(self):
        said(self.t, "assistant", QUESTION)
        said(self.t, "person", "what's UI-3 about?")
        said(self.t, "person", "never mind")
        self.reply("ok")
        self.assertEqual(self.state(), {})

    def test_a_numbered_answer_to_a_numbered_question(self):
        said(self.t, "assistant", NUMBERED)
        self.reply("ok")  # which item? nothing recorded
        self.assertEqual(self.state(), {})
        queued(self.t, "1. All ok. 2. Yes. 3. Push", uid="q1")  # sent from the phone while the session worked
        ctx = context(self.p.hook("pre-tool", {**as_agent(None, "Bash", command="ls"), "transcript_path": self.t}))
        self.assertIn("UI-1", ctx)
        self.assertEqual(sorted(self.state()), ["UI-1"])
        self.assertEqual(self.state()["UI-1"]["by"], "user (replied while the session was busy)")
        self.assertIsNone(self.p.hook("pre-tool", {**as_agent(None, "Bash", command="ls"), "transcript_path": self.t}))

    def test_in_codex(self):
        lines = [{"timestamp": now_utc(), "type": "event_msg", "payload": {"type": "agent_message", "message": QUESTION}}]
        with open(self.t, "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(x) + "\n" for x in lines))
        self.p.hook("user-prompt", {"prompt": "all ok", "transcript_path": self.t, "session_id": "c1"}, host="codex")
        self.assertTrue(self.state()["UI-1"]["passes"] and self.state()["UI-3"]["passes"])


OPEN_FAKE = r'''#!PYTHON
"""Stand-in for `claude` and `tmux` in the open tests: Claude Code asks whether to trust the folder until a key
is sent, then shows its Remote Control link."""
import json, os, sys
d = os.environ["FAKE_DIR"]
tool, args = os.path.basename(sys.argv[0]), sys.argv[1:]
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"tool": tool, "args": args}) + "\n")
trusted = os.path.join(d, "trusted")
if tool == "tmux":
    if args[0] == "list-sessions":
        print("busy-one")
    elif args[0] == "send-keys":
        open(trusted, "w").close()
    elif args[0] == "capture-pane":
        print("Remote Control: https://claude.ai/code/session_OPEN1" if os.path.exists(trusted) else
              "Do you trust the files in this folder?\n> 1. Yes, proceed\n  2. No, exit")
elif args[:2] == ["agents", "--json"]:
    print(json.dumps([{"name": "klotho-main", "sessionId": "s-1", "status": "idle"}]))
'''


class OpenSession(unittest.TestCase):
    def setUp(self):
        self.fake = tempfile.mkdtemp(prefix="regista-fake-")
        self.addCleanup(shutil.rmtree, self.fake, True)
        for tool in ("claude", "tmux"):
            path = os.path.join(self.fake, tool)
            with open(path, "w", encoding="utf-8") as f:
                f.write(OPEN_FAKE.replace("PYTHON", sys.executable))
            os.chmod(path, 0o755)
        self.folder = tempfile.mkdtemp(prefix="regista-folder-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.env = clean_env(REGISTA_CLAUDE=os.path.join(self.fake, "claude"), FAKE_DIR=self.fake,
                             REGISTA_TMUX=os.path.join(self.fake, "tmux"), REGISTA_LAUNCH_WAIT="5")

    def calls(self):
        with open(os.path.join(self.fake, "calls.jsonl"), encoding="utf-8") as f:
            return [json.loads(x) for x in f]

    def test_open_starts_a_named_session_past_the_trust_question(self):
        r = run_tool(self.folder, "open", self.folder, "klotho main", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("https://claude.ai/code/session_OPEN1", r.stdout)
        self.assertIn('"klotho-main"', r.stdout)
        self.assertIn("tmux attach -t klotho-main", r.stdout)
        calls = self.calls()
        new = next(c["args"] for c in calls if c["args"][:1] == ["new-session"])
        self.assertEqual(new[new.index("-c") + 1], self.folder)
        self.assertIn("--remote-control klotho-main", new[-1])
        self.assertIn("-n klotho-main", new[-1])
        self.assertNotIn("--permission-mode", new[-1])  # the user's own settings decide
        self.assertEqual(sum(1 for c in calls if c["args"][:1] == ["send-keys"]), 1)

    def test_open_refuses_a_name_in_use_and_a_missing_folder(self):
        r = run_tool(self.folder, "open", self.folder, "busy-one", env=self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("already running", r.stdout)
        r = run_tool(self.folder, "open", os.path.join(self.folder, "nope"), "x", env=self.env)
        self.assertIn("There's no folder", r.stdout)

    def test_agents_cant_open_sessions(self):
        p = Project()
        self.addCleanup(p.cleanup)
        for role in (None, "builder"):
            out = p.hook("pre-tool", as_agent(role, "Bash", command=f'python3 "{TOOL}" open ~/klotho klotho'))
            self.assertEqual(decision(out), "deny", role)
            self.assertIn("own terminal", reason(out))
        out = p.hook("pre-tool", as_agent(None, "Bash", command=f'python3 "{TOOL}" fleet spawn M2 --task "open the menu"'))
        self.assertNotIn("own terminal", reason(out))


class PluginSwap(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="regista-home-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.env = clean_env(HOME=self.home, CODEX_HOME=os.path.join(self.home, ".codex"))

    def test_a_move_ends_with_the_commands_that_swap_the_plugin(self):
        p = OldProject(gates=[{"name": "tests", "run": "true"}])
        self.addCleanup(p.cleanup)
        save(os.path.join(self.home, ".claude", "settings.json"), {"enabledPlugins": {"daedalus@daedalus": True}})
        save(os.path.join(self.home, ".codex", "config.toml"), '[plugins."daedalus@daedalus"]\nenabled = true\n')
        r = run_tool(p.root, "migrate", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tail = r.stdout[r.stdout.index("still enabled"):]
        for cmd in ("claude plugin uninstall daedalus@daedalus\n", "claude plugin marketplace add ciprianrosca/regista",
                    "claude plugin install regista@regista", "codex plugin add regista@regista"):
            self.assertIn(cmd, tail)
        self.assertIn('[plugins."daedalus@daedalus"]', tail)

    def test_a_project_setting_is_uninstalled_from_the_project(self):
        p = Project()
        self.addCleanup(p.cleanup)
        save(os.path.join(p.root, ".claude", "settings.json"),
             {"enabledPlugins": {"daedalus@daedalus": True, "regista@regista": True}})
        out = run_tool(p.root, "migrate", env=self.env).stdout
        self.assertIn("claude plugin uninstall daedalus@daedalus --scope project", out)
        self.assertNotIn("plugin install", out)  # Regista is enabled already

    def test_nothing_to_swap(self):
        p = Project()
        self.addCleanup(p.cleanup)
        self.assertNotIn("uninstall", run_tool(p.root, "migrate", env=self.env).stdout)


class Doctor(unittest.TestCase):
    def test_live_checks_without_a_deploy_get_a_proposal(self):
        p = Project(gates=[{"name": "tests", "run": "true"},
                           {"name": "live", "run": "node --test tests/live/", "when": ["manual"], "tests": ["tests/live/**"]}])
        self.addCleanup(p.cleanup)
        p.write("deploy/deploy.sh", "echo deploy\n")
        out = p.cli("doctor").stdout
        self.assertIn("there are live checks (live) but no deploy section", out)
        self.assertIn('"deploy": {"run": "bash deploy/deploy.sh", "timeout": 1200}', out)
        self.assertIn('set "when": ["deploy"] on live', out)

    def test_git_needs_an_author(self):
        p = Project(gates=[{"name": "tests", "run": "true"}])
        self.addCleanup(p.cleanup)
        self.assertIn("git author: T <t@example.com>", p.cli("doctor").stdout)
        sh(p.root, "git", "config", "--unset", "user.name")
        r = p.cli("doctor")
        self.assertIn("git doesn't know who commits (no user.name)", r.stdout)
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
