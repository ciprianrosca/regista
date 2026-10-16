"""End-to-end tests: each hook is driven the way Claude Code drives it (JSON on stdin), against a
throwaway git repo. Run: python -m unittest discover -s tests -v"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

DD = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts", "dd.py"))
sys.path.insert(0, os.path.dirname(DD))
import dd  # noqa: E402

PY = sys.executable


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True).stdout


class Project:
    def __init__(self, gates=None, protected=None, **cfg):
        self.root = tempfile.mkdtemp(prefix="dd-test-")
        sh(self.root, "git", "init", "-q", "-b", "main")
        sh(self.root, "git", "config", "user.email", "t@example.com")
        sh(self.root, "git", "config", "user.name", "T")
        env = {**os.environ, "CLAUDE_PROJECT_DIR": self.root}
        subprocess.run([PY, DD, "init", "--name", "Demo"], cwd=self.root, env=env, check=True, capture_output=True)
        path = os.path.join(self.root, ".daedalus", "config.json")
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        c.update(gates=gates or [], protected=protected or [], **cfg)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(c, f)
        self.write("README.md", "demo\n")
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "init")

    def write(self, rel, text):
        fp = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)

    def commit(self, rel, text, msg="work"):
        self.write(rel, text)
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", msg)

    def hook(self, event, payload=None, extra_env=None, host=None):
        data = {"cwd": self.root, **(payload or {})}
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        env.update(extra_env or {})
        r = subprocess.run([PY, DD, "hook", event, *(["--host", host] if host else [])], input=json.dumps(data),
                           capture_output=True, text=True, encoding="utf-8", env=env)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def cli(self, *args):
        return subprocess.run([PY, DD, *args], cwd=self.root, capture_output=True, text=True, encoding="utf-8")

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


def decision(out):
    return (out or {}).get("hookSpecificOutput", {}).get("permissionDecision")


class Globs(unittest.TestCase):
    def test_patterns(self):
        self.assertTrue(dd.matches("accuracy/truth/a.json", ["accuracy/truth/**"]))
        self.assertTrue(dd.matches("accuracy/truth/x/y.json", ["accuracy/truth/"]))
        self.assertFalse(dd.matches("accuracy/results/a.json", ["accuracy/truth/**"]))
        self.assertTrue(dd.matches("deep/dir/.env", [".env"]))
        self.assertTrue(dd.matches(".env", [".env"]))  # a root dotfile keeps its dot
        self.assertTrue(dd.matches("./.env", [".env"]))
        self.assertFalse(dd.matches(".env", ["env"]))
        self.assertTrue(dd.matches("src/geo/a.py", ["src/**/*.py"]))
        self.assertFalse(dd.matches("src/geo/a.txt", ["src/**/*.py"]))
        self.assertTrue(dd.matches("eval/labels/1.csv", ["eval/labels/*"]))

    def test_last_json(self):
        self.assertEqual(dd.last_json('noise\n{"a": 1}\n'), {"a": 1})
        self.assertEqual(dd.last_json('x\n{"a": 2, "b": 3}'), {"a": 2, "b": 3})
        self.assertIsNone(dd.last_json("no json here"))


class Hooks(unittest.TestCase):
    def tearDown(self):
        if getattr(self, "p", None):
            self.p.cleanup()

    def test_outside_project_is_silent(self):
        empty = tempfile.mkdtemp()
        try:
            r = subprocess.run([PY, DD, "hook", "pre-bash"], input=json.dumps({"cwd": empty, **bash("git commit -m x")}),
                               capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"})
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout.strip(), "")
        finally:
            shutil.rmtree(empty)

    def test_commit_blocked_by_failing_gate(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo boom; exit 3", "when": ["commit"]}])
        out = self.p.hook("pre-tool", bash("git add -A && git commit -m wip")) if False else \
            self.p.hook("pre-bash", bash("git add -A && git commit -m wip"))
        self.assertEqual(decision(out), "deny")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("tests: FAIL", reason)
        self.assertIn("boom", reason)

    def test_commit_allowed_when_gates_pass(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok", "when": ["commit"]}])
        self.assertIsNone(self.p.hook("pre-bash", bash('git commit -m "ok"')))

    def test_no_verify_denied(self):
        self.p = Project()
        self.assertEqual(decision(self.p.hook("pre-bash", bash("git commit --no-verify -m x"))), "deny")

    def test_git_dash_c_other_repo_ignored(self):
        self.p = Project(gates=[{"name": "tests", "run": "exit 1"}])
        other = tempfile.mkdtemp()
        try:
            self.assertIsNone(self.p.hook("pre-bash", bash(f'git -C "{other}" commit -m x')))
        finally:
            shutil.rmtree(other)

    def test_push_asks(self):
        self.p = Project()
        self.assertEqual(decision(self.p.hook("pre-bash", bash("git push origin main"))), "ask")

    def test_push_block(self):
        self.p = Project(push="block")
        self.assertEqual(decision(self.p.hook("pre-bash", bash("git push"))), "deny")

    def test_powershell_tool_same_rules(self):
        self.p = Project(gates=[{"name": "tests", "run": "exit 1"}])
        out = self.p.hook("pre-bash", {"tool_name": "PowerShell", "tool_input": {"command": "git commit -m x"}})
        self.assertEqual(decision(out), "deny")

    def test_protected_edit_denied(self):
        self.p = Project(protected=["accuracy/truth/**"])
        path = os.path.join(self.p.root, "accuracy", "truth", "a.json")
        self.assertEqual(decision(self.p.hook("pre-edit", {"tool_name": "Write", "tool_input": {"file_path": path}})), "deny")
        ok = os.path.join(self.p.root, "src", "a.py")
        self.assertIsNone(self.p.hook("pre-edit", {"tool_name": "Edit", "tool_input": {"file_path": ok}}))

    def test_protected_shell_write_denied(self):
        self.p = Project(protected=["accuracy/truth/**"])
        self.assertEqual(decision(self.p.hook("pre-bash", bash("rm -rf accuracy/truth"))), "deny")
        self.assertEqual(decision(self.p.hook("pre-bash", bash("echo 1 > accuracy/truth/x.json"))), "deny")
        self.assertIsNone(self.p.hook("pre-bash", bash("cat accuracy/truth/x.json")))

    def test_baseline_edit_denied(self):
        self.p = Project()
        path = os.path.join(self.p.root, ".daedalus", "gates", "baseline.json")
        self.assertEqual(decision(self.p.hook("pre-edit", {"tool_name": "Write", "tool_input": {"file_path": path}})), "deny")

    def test_metric_gate_thresholds_and_no_regression(self):
        g = {"name": "acc", "run": "python -c \"print('{\\\"p95\\\": %s}' % open('v.txt').read().strip())\"",
             "when": ["commit"], "metrics": {"p95": {"max": 1.5}}}
        self.p = Project(gates=[g])
        self.p.write("v.txt", "1.2")
        r = self.p.cli("gate", "--accept")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.p.write("v.txt", "1.3")  # under the max, but worse than the 1.2 baseline
        out = self.p.hook("pre-bash", bash("git commit -am x"))
        self.assertEqual(decision(out), "deny")
        self.assertIn("worse than the baseline", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.p.write("v.txt", "1.7")  # over the max
        self.assertIn("> max 1.5", self.p.cli("gate").stdout)
        self.p.write("v.txt", "1.1")  # better: passes
        self.assertEqual(self.p.cli("gate").returncode, 0)

    def test_paths_filter_skips_expensive_gate(self):
        self.p = Project(gates=[{"name": "geo", "run": "exit 1", "paths": ["src/geo/**"]}])
        self.p.write("docs/x.md", "doc")
        self.assertIsNone(self.p.hook("pre-bash", bash("git commit -am docs")))
        self.p.write("src/geo/a.py", "x = 1")
        self.assertEqual(decision(self.p.hook("pre-bash", bash("git commit -am geo"))), "deny")

    def test_gate_timeout_counts_as_failure(self):
        self.p = Project(gates=[{"name": "slow", "run": "sleep 5", "timeout": 1}])
        out = self.p.hook("pre-bash", bash("git commit -m x"))
        self.assertEqual(decision(out), "deny")

    def test_stop_blocks_unsaved_commits_once(self):
        self.p = Project()
        self.p.write(".daedalus/state/2026-01-01-first.md", "# h\n## Resume point\nstart\n")
        time.sleep(1.1)
        self.p.commit("src/a.py", "x = 1", "feat: a")
        out = self.p.hook("stop", {"stop_hook_active": False})
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("feat: a", out["reason"])
        self.assertIsNone(self.p.hook("stop", {"stop_hook_active": True}))

    def test_stop_ok_after_handoff_and_bookkeeping_commit(self):
        self.p = Project()
        self.p.commit("src/a.py", "x = 1", "feat: a")
        time.sleep(1.1)
        self.p.write(".daedalus/state/2026-01-02-save.md", "# h\n## Resume point\nnext\n")
        self.assertIsNone(self.p.hook("stop", {}))
        time.sleep(1.1)
        self.p.commit(".daedalus/status.md", "updated", "docs: save")  # committing the save itself
        self.assertIsNone(self.p.hook("stop", {}))

    def test_stop_remind_mode(self):
        self.p = Project(stop="remind")
        self.p.commit("src/a.py", "x = 1")
        out = self.p.hook("stop", {})
        self.assertNotIn("decision", out)
        self.assertIn("systemMessage", out)

    def test_session_start_context(self):
        self.p = Project()
        self.p.write(".daedalus/state/2026-01-03-x.md", "# h\n## Resume point\nWire the exporter.\n## Blocked on\n- nothing\n")
        out = self.p.hook("session-start", {"source": "startup"})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Wire the exporter.", ctx)
        self.assertIn("dd.py", ctx)
        self.assertLess(len(ctx), 10000)
        out = self.p.hook("session-start", {"source": "compact"})
        self.assertIn("compacted", out["hookSpecificOutput"]["additionalContext"])

    def test_snapshot_at_session_end(self):
        self.p = Project()
        self.p.write(".daedalus/state/2026-01-04-x.md", "# h\n")
        time.sleep(1.1)
        self.p.commit("src/b.py", "y = 2", "feat: b")
        self.p.hook("session-end", {"reason": "exit"})
        autos = [f for f in os.listdir(os.path.join(self.p.root, ".daedalus", "state")) if "-auto-" in f]
        self.assertEqual(len(autos), 1)
        self.p.hook("session-end", {"reason": "exit"})  # nothing new: no second snapshot
        autos = [f for f in os.listdir(os.path.join(self.p.root, ".daedalus", "state")) if "-auto-" in f]
        self.assertEqual(len(autos), 1)

    def test_state_readme_is_not_a_handoff(self):
        self.p = Project()
        self.assertIsNone(dd.newest_handoff(self.p.root, dd.load_config(self.p.root)))


def transcript(path, tokens, model="claude-sonnet-4-6"):
    """A minimal transcript: one main-thread model call that used `tokens` of input, plus a
    subagent call that must be ignored."""
    rows = [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "message": {"model": model, "usage": {
            "input_tokens": 10, "cache_read_input_tokens": tokens - 110, "cache_creation_input_tokens": 100,
            "output_tokens": 999}}},
        {"type": "assistant", "isSidechain": True, "message": {"model": model, "usage": {
            "input_tokens": 5, "cache_read_input_tokens": 1, "cache_creation_input_tokens": 0, "output_tokens": 1}}},
    ]
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


PAUSE = {"on_limit": "pause"}


class ContextBudget(unittest.TestCase):
    """The pause mode (on_limit: pause): warn 55 %, limit 65 %. claude-sonnet-4-6 has a 200k window,
    so 65 % = 130k tokens."""

    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}], context=PAUSE)
        self.t = os.path.join(self.p.root, "..", os.path.basename(self.p.root) + "-transcript.jsonl")

    def tearDown(self):
        self.p.cleanup()
        if os.path.exists(self.t):
            os.remove(self.t)

    def call(self, event, tokens, model="claude-sonnet-4-6", session="s1", **payload):
        transcript(self.t, tokens, model)
        return self.p.hook(event, {"transcript_path": self.t, "session_id": session, **payload})

    def tool(self, tokens, name, **ti):
        return self.call("pre-tool", tokens, tool_name=name, tool_input=ti)

    def test_window_from_model(self):
        cfg = dd.load_config(self.p.root)
        self.assertEqual(dd.context_window(cfg, 1000, "claude-opus-5-5"), 1_000_000)
        self.assertEqual(dd.context_window(cfg, 1000, "claude-sonnet-5-5"), 1_000_000)
        self.assertEqual(dd.context_window(cfg, 1000, "claude-fable-5-1"), 1_000_000)
        self.assertEqual(dd.context_window(cfg, 1000, "claude-opus-4-7"), 1_000_000)
        self.assertEqual(dd.context_window(cfg, 1000, "claude-opus-4-6"), 200_000)
        self.assertEqual(dd.context_window(cfg, 1000, "claude-opus-4-6[1m]"), 1_000_000)
        self.assertEqual(dd.context_window(cfg, 250_000, "unknown-model"), 1_000_000)
        cfg["context"]["window"] = 400_000
        self.assertEqual(dd.context_window(cfg, 1000, "claude-opus-5-5"), 400_000)

    def test_tokens_ignore_output_and_subagents(self):
        transcript(self.t, 120_000)
        self.assertEqual(dd.context_tokens(self.t), (120_000, "claude-sonnet-4-6"))

    def test_below_warn_is_silent(self):
        self.assertIsNone(self.tool(60_000, "Read", file_path=os.path.join(self.p.root, "README.md")))

    def test_warn_note_once_per_band(self):
        out = self.tool(112_000, "Read", file_path=os.path.join(self.p.root, "README.md"))  # 56 %
        self.assertIn("Finish the current step", out["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
        self.assertIsNone(self.tool(113_000, "Read", file_path=os.path.join(self.p.root, "README.md")))  # 56.5 %: no repeat
        self.assertIsNotNone(self.tool(118_000, "Read", file_path=os.path.join(self.p.root, "README.md")))  # 59 %

    def test_over_limit_only_saving_is_allowed(self):
        over = 132_000  # 66 %
        self.assertEqual(decision(self.tool(over, "Read", file_path=os.path.join(self.p.root, "src", "a.py"))), "deny")
        self.assertEqual(decision(self.tool(over, "Bash", command="pytest -q")), "deny")
        self.assertEqual(decision(self.tool(over, "Agent", prompt="x")), "deny")
        self.assertEqual(decision(self.tool(over, "WebFetch", url="https://example.com")), "deny")
        self.assertIsNone(self.tool(over, "Skill", skill="daedalus:pause"))
        self.assertIsNone(self.tool(over, "Write", file_path=os.path.join(self.p.root, ".daedalus", "state", "2026-10-09-pause.md")))
        self.assertIsNone(self.tool(over, "Edit", file_path=os.path.join(self.p.root, ".daedalus", "status.md")))
        self.assertIsNone(self.tool(over, "Bash", command="git status --short && git add -A && git commit -m wip"))
        self.assertIsNone(self.tool(over, "Bash", command=f'python "{DD}" gate'))
        self.assertEqual(decision(self.tool(over, "Bash", command="git status && rm -rf src")), "deny")

    def test_gates_still_apply_while_paused(self):
        self.p = Project(gates=[{"name": "tests", "run": "exit 1"}], context=PAUSE)
        self.assertEqual(decision(self.tool(132_000, "Bash", command="git commit -am wip")), "deny")

    def test_stop_blocked_until_pause_handoff_written(self):
        self.tool(132_000, "Read", file_path="x")  # crossing the limit records the moment
        out = self.call("stop", 132_000)
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("/daedalus:pause", out["reason"])
        time.sleep(1.1)
        self.p.write(".daedalus/state/2026-10-09-pause-x.md", "# h\n## Resume prompt\n/daedalus:start continue\n")
        self.assertIsNone(self.call("stop", 132_000))

    def test_stop_hook_active_never_loops(self):
        self.assertIsNone(self.call("stop", 132_000, stop_hook_active=True))

    def test_user_prompt_over_limit(self):
        out = self.call("user-prompt", 140_000, prompt="next feature please")
        self.assertIn("/daedalus:pause", out["hookSpecificOutput"]["additionalContext"])

    def test_new_session_starts_fresh(self):
        self.tool(132_000, "Read", file_path="x")
        # after /clear: a new session id with a small context
        self.assertIsNone(self.call("pre-tool", 20_000, session="s2", tool_name="Read",
                                    tool_input={"file_path": os.path.join(self.p.root, "README.md")}))
        out = self.call("stop", 20_000, session="s2") or {}
        self.assertNotIn("context", out.get("reason", ""))  # only the unrelated unsaved-commit rule may fire

    def test_one_million_window(self):
        self.assertIsNone(self.call("pre-tool", 500_000, model="claude-opus-5-5", tool_name="Bash",
                                    tool_input={"command": "pytest -q"}))  # 50 % of 1M
        self.assertEqual(decision(self.call("pre-tool", 660_000, model="claude-opus-5-5", tool_name="Bash",
                                            tool_input={"command": "pytest -q"})), "deny")


class CompactMode(unittest.TestCase):
    """The default (on_limit: compact): the host compacts at 65 %, the work carries on; the hard pause
    starts only at 75 %, if compaction didn't happen."""

    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}])
        self.t = os.path.join(self.p.root, "..", os.path.basename(self.p.root) + "-transcript.jsonl")

    def tearDown(self):
        self.p.cleanup()
        if os.path.exists(self.t):
            os.remove(self.t)

    def call(self, event, tokens, **payload):
        transcript(self.t, tokens)
        return self.p.hook(event, {"transcript_path": self.t, "session_id": "s1", **payload})

    def bash(self, tokens, cmd="pytest -q"):
        return self.call("pre-tool", tokens, tool_name="Bash", tool_input={"command": cmd})

    def test_warning_asks_for_a_checkpoint_not_a_pause(self):
        note = self.bash(112_000)["hookSpecificOutput"]["additionalContext"]  # 56 %
        self.assertIn("compacts itself", note)
        self.assertIn("/daedalus:save", note)
        self.assertNotIn("/clear", note)

    def test_over_the_limit_work_continues(self):
        self.assertIsNone(decision(self.bash(132_000)))  # 66 %: compaction is due, nothing is refused
        out = self.call("stop", 132_000) or {}
        self.assertNotIn("pause", out.get("reason", ""))
        self.assertIsNone(self.call("user-prompt", 132_000, prompt="go on"))

    def test_backstop_when_compaction_didnt_happen(self):
        out = self.bash(152_000)  # 76 %
        self.assertEqual(decision(out), "deny")
        self.assertIn("should have compacted", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIn("doctor", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_after_compaction_carry_on(self):
        ctx = self.p.hook("session-start", {"source": "compact"})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("carry on", ctx)
        self.assertNotIn("Where we are / Next", ctx)

    def test_compaction_is_logged(self):
        self.call("pre-compact", 131_000, trigger="auto")
        rows = dd.read(os.path.join(self.p.root, ".daedalus", "compactions.jsonl")).strip().splitlines()
        self.assertEqual(json.loads(rows[-1])["trigger"], "auto")

    def test_subagent_backstop_too(self):
        sub = {"agent_id": "a1", "agent_type": "daedalus:builder", "tool_name": "Bash", "tool_input": {"command": "ls"}}
        transcript(self.t, 10_000)
        path = os.path.join(os.path.splitext(self.t)[0], "subagents", "agent-a1.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            for tokens, want in ((140_000, None), (152_000, "deny")):  # 70 %, 76 %
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps({"type": "assistant", "isSidechain": True, "message": {
                        "model": "claude-sonnet-4-6", "usage": {"input_tokens": tokens}}}) + "\n")
                out = self.p.hook("pre-tool", {"transcript_path": self.t, **sub})
                self.assertEqual(decision(out), want)
        finally:
            shutil.rmtree(os.path.splitext(self.t)[0], ignore_errors=True)

    def test_setup_writes_claude_compaction_and_doctor_checks_it(self):
        self.assertNotEqual(self.p.cli("doctor").returncode, 0)
        self.p.write(".claude/settings.json", json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
        r = self.p.cli("setup")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = dd.load_json(os.path.join(self.p.root, ".claude", "settings.json"), {})
        self.assertEqual(data["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "65")
        self.assertEqual(data["permissions"], {"allow": ["Bash(ls)"]})  # the rest is kept
        r = self.p.cli("doctor")
        self.assertIn("Claude Code compacts at 65%", r.stdout)


def as_agent(role, tool, **ti):
    """Hook payload for a tool call made inside one of our agents (or the main session if role is None)."""
    d = {"tool_name": tool, "tool_input": ti}
    if role:
        d.update(agent_id="a1", agent_type=role if ":" in role or role in ("Explore", "general-purpose") else f"daedalus:{role}")
    return d


class Roles(unittest.TestCase):
    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}])

    def tearDown(self):
        self.p.cleanup()

    def f(self, rel):
        return os.path.join(self.p.root, *rel.split("/"))

    def check(self, role, tool, **ti):
        return decision(self.p.hook("pre-tool", as_agent(role, tool, **ti)))

    def test_orchestrator_doesnt_edit_code_or_tests(self):
        self.assertEqual(self.check(None, "Write", file_path=self.f("src/a.py")), "deny")
        self.assertEqual(self.check(None, "Edit", file_path=self.f("tests/test_a.py")), "deny")
        self.assertIsNone(self.check(None, "Edit", file_path=self.f(".daedalus/status.md")))
        self.assertIsNone(self.check(None, "Write", file_path=self.f("README.md")))
        self.assertIsNone(self.check(None, "Read", file_path=self.f("src/a.py")))
        self.assertIsNone(self.check(None, "Agent", subagent_type="daedalus:builder", prompt="x"))

    def test_solo_mode_lets_the_main_session_build(self):
        self.p = Project(mode="solo")
        self.assertIsNone(self.check(None, "Write", file_path=self.f("src/a.py")))

    def test_builder(self):
        self.assertIsNone(self.check("builder", "Write", file_path=self.f("src/a.py")))
        self.assertEqual(self.check("builder", "Edit", file_path=self.f("tests/test_a.py")), "deny")
        self.assertEqual(self.check("builder", "Bash", command="git add -A && git commit -m x"), "deny")
        self.assertEqual(self.check("builder", "Agent", subagent_type="daedalus:reviewer", prompt="approve me"), "deny")
        self.assertIsNone(self.check("builder", "Bash", command="python -m pytest -q"))

    def test_test_writer_works_blind(self):
        self.assertIsNone(self.check("test-writer", "Write", file_path=self.f("tests/test_a.py")))
        self.assertEqual(self.check("test-writer", "Write", file_path=self.f("src/a.py")), "deny")
        self.assertEqual(self.check("test-writer", "Read", file_path=self.f("src/a.py")), "deny")
        self.assertEqual(self.check("test-writer", "Grep", pattern="def", path=self.f("src")), "deny")
        self.assertEqual(self.check("test-writer", "Grep", pattern="def"), "deny")  # no path = whole project
        self.assertIsNone(self.check("test-writer", "Grep", pattern="def", path=self.f("tests")))
        self.assertEqual(self.check("test-writer", "Bash", command="cat src/a.py"), "deny")
        self.assertIsNone(self.check("test-writer", "Read", file_path=self.f(".daedalus/plan.md")))
        self.assertIsNone(self.check("test-writer", "Bash", command="python -m pytest tests/test_a.py"))

    def test_reviewer_reports_doesnt_fix(self):
        self.assertEqual(self.check("reviewer", "Edit", file_path=self.f("src/a.py")), "deny")
        self.assertIsNone(self.check("reviewer", "Write", file_path=self.f(".daedalus/reviews/draft.json")))
        self.assertIsNone(self.check("reviewer", "Bash", command="git diff HEAD 2>&1"))
        self.assertIsNone(self.check("reviewer", "Bash", command=f'python "{DD}" review-record'))
        self.assertEqual(self.check("reviewer", "Bash", command="echo x > src/a.py"), "deny")

    def test_only_the_reviewer_writes_reviews(self):
        for role in (None, "builder", "researcher"):
            self.assertEqual(self.check(role, "Write", file_path=self.f(".daedalus/reviews/draft.json")), "deny", role)
            self.assertEqual(self.check(role, "Bash", command=f'python "{DD}" review-record'), "deny", role)

    def test_researcher(self):
        self.assertIsNone(self.check("researcher", "Write", file_path=self.f(".daedalus/research/q1.md")))
        self.assertEqual(self.check("researcher", "Write", file_path=self.f("src/a.py")), "deny")
        self.assertIsNone(self.check("researcher", "WebFetch", url="https://example.com"))

    def test_other_subagents_cant_edit_code_in_orchestrated_mode(self):
        self.assertEqual(self.check("general-purpose", "Write", file_path=self.f("src/a.py")), "deny")
        self.assertIsNone(self.check("Explore", "Read", file_path=self.f("src/a.py")))

    def test_spoofed_role_name_is_just_other(self):
        self.assertEqual(dd.role_of({"agent_id": "x", "agent_type": "reviewer"}), "other")
        self.assertEqual(dd.role_of({"agent_id": "x", "agent_type": "daedalus:reviewer"}), "reviewer")
        self.assertEqual(dd.role_of({}), "main")

    def test_subagent_start_injects_role(self):
        out = self.p.hook("subagent-start", {"agent_id": "a9", "agent_type": "daedalus:test-writer"})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("test-writer", ctx)
        self.assertIn("dd.py", ctx)
        self.assertIsNone(self.p.hook("subagent-start", {"agent_id": "a8", "agent_type": "Explore"}))


BIG = "".join(f"def f{i}(x):\n    return x + {i}\n" for i in range(10))  # 20 lines


class ReviewGate(unittest.TestCase):
    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}])

    def tearDown(self):
        self.p.cleanup()

    def commit_check(self):
        return self.p.hook("pre-tool", as_agent(None, "Bash", command="git add -A && git commit -m feat"))

    def draft(self, verdict="approve", findings=None, checked=("spec", "edge cases")):
        self.p.write(".daedalus/reviews/draft.json", json.dumps(
            {"verdict": verdict, "summary": "s", "checked": list(checked), "findings": findings or []}))

    def test_small_change_needs_no_review(self):
        self.p.write("src/a.py", "x = 1\n")
        self.assertIsNone(self.commit_check())

    def test_big_change_needs_an_approving_review_of_exactly_this_diff(self):
        self.p.write("src/a.py", BIG)
        out = self.commit_check()
        self.assertEqual(decision(out), "deny")
        self.assertIn("no review", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.draft()
        r = self.p.cli("review-record")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNone(self.commit_check())
        self.p.write("src/a.py", BIG + "x = 2\n")  # edited after the review
        self.assertEqual(decision(self.commit_check()), "deny")

    def test_build_output_doesnt_void_a_review(self):
        self.p.write("src/a.py", BIG)
        self.draft()
        self.assertEqual(self.p.cli("review-record").returncode, 0)
        self.p.write("src/__pycache__/a.cpython-312.pyc", "bytecode, rewritten by every test run")
        self.assertIsNone(self.commit_check())

    def test_compound_commands_checked_per_segment(self):
        # Seen live: `sed -i` on the plan + `git add` of the verdict + commit was refused as a review write.
        self.assertEqual(dd.writes_to("sed -i 's/x/y/' .daedalus/plan.md && git add -A .daedalus/reviews/a.json && "
                                      "git commit -m x", [".daedalus/reviews"]), [])
        self.assertEqual(dd.writes_to("git status && cp a.json .daedalus/reviews/b.json", [".daedalus/reviews"]),
                         [".daedalus/reviews"])
        self.assertIsNone(self.p.hook("pre-tool", as_agent(None, "Bash", command=(
            "sed -i 's/x/y/' .daedalus/plan.md && git add -A .daedalus/reviews/a.json && git commit -m x"))))

    def test_changes_verdict_blocks(self):
        self.p.write("src/a.py", BIG)
        self.draft("changes", [{"severity": "major", "file": "src/a.py", "line": 3, "issue": "off by one"}])
        self.assertEqual(self.p.cli("review-record").returncode, 0)
        out = self.commit_check()
        self.assertEqual(decision(out), "deny")
        self.assertIn("off by one", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_record_validates_the_verdict(self):
        self.p.write("src/a.py", BIG)
        self.draft("approve", [{"severity": "blocker", "file": "src/a.py", "line": 1, "issue": "x"}])
        self.assertNotEqual(self.p.cli("review-record").returncode, 0)  # approve + blocker
        self.draft("approve", checked=())
        self.assertNotEqual(self.p.cli("review-record").returncode, 0)  # nothing checked
        self.draft("changes")
        self.assertNotEqual(self.p.cli("review-record").returncode, 0)  # changes without findings
        self.draft("approve", [{"severity": "minor", "file": "src/a.py", "issue": "no line"}])
        self.assertNotEqual(self.p.cli("review-record").returncode, 0)  # finding without a line

    def test_reviewer_must_record_before_finishing(self):
        self.p.hook("subagent-start", {"agent_id": "r1", "agent_type": "daedalus:reviewer"})
        out = self.p.hook("subagent-stop", {"agent_id": "r1", "agent_type": "daedalus:reviewer"})
        self.assertEqual(out.get("decision"), "block")
        self.assertIsNone(self.p.hook("subagent-stop", {"agent_id": "r1", "agent_type": "daedalus:reviewer",
                                                       "stop_hook_active": True}))
        self.p.write("src/a.py", BIG)
        time.sleep(1.1)
        self.draft()
        self.assertEqual(self.p.cli("review-record").returncode, 0)
        self.assertIsNone(self.p.hook("subagent-stop", {"agent_id": "r1", "agent_type": "daedalus:reviewer"}))


class TestIntegrity(unittest.TestCase):
    """Improvement 1: the builder can't change tests or what decides them, by any route."""

    def setUp(self):
        self.p = Project()

    def tearDown(self):
        self.p.cleanup()

    def check(self, role, tool, **ti):
        return decision(self.p.hook("pre-tool", as_agent(role, tool, **ti)))

    def test_builder_cant_touch_tests_from_the_shell(self):
        for cmd in ("sed -i 's/4/5/' tests/test_a.py", "echo 'pass' > tests/test_a.py", "rm tests/test_a.py",
                    "echo '[pytest]' >> pytest.ini", "cp x.py src/conftest.py", "rm -rf src/__snapshots__"):
            self.assertEqual(self.check("builder", "Bash", command=cmd), "deny", cmd)
        for cmd in ("python -m pytest -q", "python -m pytest -q > /dev/null", "cat tests/test_a.py",
                    "echo 'x = 1' > src/a.py", "echo x > build/latest/out.txt"):
            self.assertIsNone(self.check("builder", "Bash", command=cmd), cmd)

    def test_gate_scripts_are_guarded_like_tests(self):
        self.p = Project(gates=[{"name": "e2e", "run": "bash e2e/run.sh", "evidence": "e2e", "tests": ["e2e/**"]}])
        self.p.write("e2e/run.sh", "exit 0\n")
        self.assertEqual(self.check("builder", "Edit", file_path=os.path.join(self.p.root, "e2e", "run.sh")), "deny")
        self.assertEqual(self.check("builder", "Bash", command="echo 'exit 0' > e2e/run.sh"), "deny")
        self.assertIsNone(self.check("test-writer", "Edit", file_path=os.path.join(self.p.root, "e2e", "run.sh")))

    def test_reviewer_may_use_scratch_files(self):
        self.assertIsNone(self.check("reviewer", "Bash",
                                     command="printf 'a b\\n' > /tmp/x.txt && PYTHONPATH=src python -m wc /tmp/x.txt"))
        self.assertIsNone(self.check("reviewer", "Bash", command='echo hi > "$TMPDIR/probe.txt"'))
        self.assertEqual(self.check("reviewer", "Bash", command="echo hi > src/a.py"), "deny")

    def test_builder_cant_edit_test_config(self):
        for path in ("pytest.ini", "src/conftest.py", "web/vitest.config.ts", "src/__snapshots__/a.snap"):
            self.assertEqual(self.check("builder", "Write", file_path=os.path.join(self.p.root, path)), "deny", path)

    def test_test_writer_owns_test_config(self):
        self.assertIsNone(self.check("test-writer", "Write", file_path=os.path.join(self.p.root, "tests", "conftest.py")))

    def test_harness_files_belong_to_the_harness(self):
        cfgfile = os.path.join(self.p.root, ".daedalus", "config.json")
        self.assertEqual(self.check(None, "Edit", file_path=cfgfile), "ask")  # the user decides
        self.assertEqual(self.check("builder", "Edit", file_path=cfgfile), "deny")
        self.assertEqual(self.check(None, "Bash", command="sed -i 's/65/95/' .daedalus/config.json"), "ask")
        for role in (None, "builder", "reviewer"):
            self.assertEqual(self.check(role, "Write", file_path=os.path.join(
                self.p.root, ".daedalus", "gates", "requirements.json")), "deny", role)
        self.assertEqual(self.check(None, "Bash", command="echo {} > .daedalus/gates/baseline.json"), "deny")
        self.assertEqual(self.check(None, "Bash", command=f'python "{DD}" verify-manual UI-1 --note ok'), "deny")
        self.assertIsNone(self.check(None, "Bash", command=f'python "{DD}" gate'))


class FailClosed(unittest.TestCase):
    """Improvement 2: a broken guard blocks; it never waves the action through."""

    def setUp(self):
        self.p = Project()
        self.p.write(".daedalus/config.json", "{ not json")

    def tearDown(self):
        self.p.cleanup()

    def test_broken_config_blocks_tool_calls(self):
        out = self.p.hook("pre-tool", as_agent(None, "Bash", command="git commit -m x"))
        self.assertEqual(decision(out), "deny")
        self.assertIn("Failing closed", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_escape_hatch(self):
        self.assertIsNone(self.p.hook("pre-tool", as_agent(None, "Bash", command="ls"),
                                      extra_env={"DAEDALUS_FAIL_OPEN": "1"}))

    def test_other_events_dont_trap_the_session(self):
        self.assertIsNone(self.p.hook("stop", {}))
        self.assertIsNone(self.p.hook("session-start", {"source": "startup"}))

    def test_gate_budget(self):
        self.p.write(".daedalus/config.json", "{}")
        gates = [{"name": "slow", "run": "sleep 3", "timeout": 60}, {"name": "next", "run": "echo ok"}]
        res = dd.run_gates(self.p.root, dd.load_config(self.p.root), gates, budget=2)
        self.assertEqual([r["status"] for r in res], ["fail", "fail"])  # cut at the budget, then no time left
        self.assertIn("no time left", res[1]["fails"][0])


SPEC = {"step": "stats", "milestone": "M1", "title": "Statistics", "requirements": [
    {"id": "STATS-1", "text": "WHEN the list is empty mean SHALL raise ValueError", "verify": "test"},
    {"id": "STATS-2", "text": "WHEN run from the CLI the tool SHALL print the mean", "verify": "e2e"}]}
E2E_GATE = {"name": "e2e", "run": "echo ok", "when": ["commit"], "evidence": "e2e", "tests": ["e2e/**"]}


class Requirements(unittest.TestCase):
    """Improvements 3 and 4: requirements pass only through the gates; milestones need end-to-end proof."""

    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}, E2E_GATE])
        self.p.write(".daedalus/specs/stats.json", json.dumps(SPEC))

    def tearDown(self):
        self.p.cleanup()

    def commit(self):
        return self.p.hook("pre-tool", as_agent(None, "Bash", command="git add -A && git commit -m feat"))

    def state(self):
        return dd.load_json(dd.requirements_path(self.p.root), {})

    def test_uncovered_requirements_block_the_commit(self):
        out = self.commit()
        self.assertEqual(decision(out), "deny")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("STATS-1", reason)
        self.assertIn("STATS-2", reason)
        self.p.write("tests/test_stats.py", "# covers: STATS-1\n")
        self.assertIn("STATS-2", self.commit()["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertNotIn("STATS-1 (", self.commit()["hookSpecificOutput"]["permissionDecisionReason"])

    def test_gates_flip_passes_and_the_milestone(self):
        self.p.write("tests/test_stats.py", "# covers: STATS-1\n")
        self.p.write("e2e/cli_test.sh", "# covers: STATS-2\n")
        done, why = dd.milestone_status(self.p.root, dd.load_config(self.p.root), "M1")
        self.assertFalse(done)
        self.assertIsNone(self.commit())
        self.assertTrue(self.state()["STATS-1"]["passes"])
        self.assertTrue(self.state()["STATS-2"]["passes"])
        self.assertEqual(self.p.cli("milestone", "M1").returncode, 0)

    def test_a_failing_gate_fails_its_requirements(self):
        self.p = Project(gates=[{"name": "tests", "run": "exit 1"}, E2E_GATE])
        self.p.write(".daedalus/specs/stats.json", json.dumps(SPEC))
        self.p.write("tests/test_stats.py", "# covers: STATS-1\n")
        self.p.write("e2e/cli_test.sh", "# covers: STATS-2\n")
        self.assertEqual(decision(self.commit()), "deny")
        self.assertFalse(self.state()["STATS-1"]["passes"])
        self.assertNotEqual(self.p.cli("milestone", "M1").returncode, 0)

    def test_milestone_needs_end_to_end_evidence(self):
        spec = {**SPEC, "requirements": [SPEC["requirements"][0]]}  # unit-tested only
        self.p.write(".daedalus/specs/stats.json", json.dumps(spec))
        self.p.write("tests/test_stats.py", "# covers: STATS-1\n")
        self.assertIsNone(self.commit())
        done, why = dd.milestone_status(self.p.root, dd.load_config(self.p.root), "M1")
        self.assertFalse(done)
        self.assertIn("end-to-end", " ".join(why))

    def test_planned_specs_dont_block(self):
        self.p.write(".daedalus/specs/stats.json", json.dumps({**SPEC, "state": "planned"}))
        self.assertIsNone(self.commit())

    def test_bad_specs_are_reported(self):
        bad = {**SPEC, "requirements": [{"id": "stats one", "text": "", "verify": "vibes"}, SPEC["requirements"][0]]}
        self.p.write(".daedalus/specs/stats.json", json.dumps(bad))
        self.p.write(".daedalus/specs/dup.json", json.dumps({**SPEC, "step": "dup"}))
        _, problems = dd.load_specs(self.p.root)
        text = " ".join(problems)
        self.assertIn("must look like NAME-1", text)
        self.assertIn("also defined", text)
        self.assertIn("verify must be", text)

    def test_id_matching_is_exact(self):
        self.p.write("tests/test_stats.py", "# covers: STATS-10, XSTATS-1\n")
        cov = dd.coverage(self.p.root, dd.load_config(self.p.root), [SPEC])
        self.assertEqual(cov["STATS-1"]["tests"], [])

    def test_manual_requirements_need_the_user(self):
        spec = {**SPEC, "requirements": [{"id": "UX-1", "text": "The page SHALL feel fast on a phone", "verify": "manual"}]}
        self.p.write(".daedalus/specs/ux.json", json.dumps({**spec, "milestone": "M2"}))
        self.assertNotEqual(self.p.cli("milestone", "M2").returncode, 0)
        r = self.p.cli("verify-manual", "UX-1", "--note", "checked on an iPhone 16")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.state()["UX-1"]["passes"])


TRY_SPEC = {"step": "signup", "milestone": "M1", "title": "Sign-up", "requirements": [
    {"id": "UI-1", "text": "WHEN a visitor signs up on a phone THEN the page SHALL fit the screen", "verify": "manual",
     "try": {"title": "Sign-up fits a phone screen", "steps": ["Run `npm run dev`", "Open /signup on your phone"],
             "expect": "No sideways scrolling"}},
    {"id": "UI-2", "text": "WHEN the form is sent the user SHALL land on the dashboard", "verify": "e2e",
     "try": {"steps": ["Sign up"], "expect": "The dashboard"}},
    {"id": "UI-3", "text": "The colours SHALL match the brand", "verify": "manual"}]}


class TryItYourself(unittest.TestCase):
    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}])
        self.p.write(".daedalus/specs/signup.json", json.dumps(TRY_SPEC))

    def tearDown(self):
        self.p.cleanup()

    def state(self):
        return dd.load_json(dd.requirements_path(self.p.root), {})

    def typed(self, args, **extra):
        return self.p.hook("user-prompt-expansion", {"command_name": "daedalus:verify", "command_args": args,
                                                     "prompt": f"/daedalus:verify {args}", **extra})

    def test_checklist(self):
        out = self.p.cli("try", "M1").stdout
        self.assertIn("2 check(s) waiting for you", out)
        self.assertIn("Sign-up fits a phone screen", out)
        self.assertIn("1. Run `npm run dev`", out)
        self.assertIn("You should see: No sideways scrolling", out)
        self.assertIn("optional: the gates already prove this one", out)
        self.assertIn("no steps written yet", out)  # UI-3 has no try steps
        self.assertIn("/daedalus:verify UI-1 ok", out)
        self.assertIn("UI-1", self.p.cli("status").stdout)

    def test_user_typed_verify_records(self):
        ctx = self.typed("UI-1 ok fits on my Pixel")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("recorded UI-1 as verified", ctx)
        self.assertTrue(self.state()["UI-1"]["passes"])
        self.assertEqual(self.state()["UI-1"]["evidence"], ["fits on my Pixel"])
        ctx = self.typed("ui-3 fail the button is purple")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("NOT working", ctx)
        self.assertFalse(self.state()["UI-3"]["passes"])
        self.assertIn("you reported a problem", self.p.cli("try").stdout)

    def test_unclear_or_wrong_commands_record_nothing(self):
        self.assertIn("needs ok or fail", self.typed("UI-1 looks fine I guess")["hookSpecificOutput"]["additionalContext"])
        self.assertIn("no requirement UI-9", self.typed("UI-9 ok")["hookSpecificOutput"]["additionalContext"])
        self.assertIn("proven by the gates", self.typed("UI-2 ok")["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.state(), {})
        self.assertIsNone(self.p.hook("user-prompt-expansion", {"command_name": "other:verify", "command_args": "UI-1 ok",
                                                                "prompt": "/other:verify UI-1 ok"}))

    def test_agents_cant_record_checks(self):
        for payload in (as_agent(None, "Skill", skill="daedalus:verify", args="UI-1 ok"),
                        as_agent(None, "Bash", command=f'python "{DD}" try --ask'),
                        as_agent("builder", "Bash", command=f'echo y | python "{DD}" try M1 --ask'),
                        as_agent(None, "Bash", command=f'python "{DD}" verify-manual UI-1 --note x')):
            self.assertEqual(decision(self.p.hook("pre-tool", payload)), "deny", payload)
        self.assertIsNone(self.p.hook("pre-tool", as_agent(None, "Bash", command=f'python "{DD}" try M1')))
        # a subagent's expansion never records
        self.assertIsNone(self.typed("UI-1 ok", agent_id="a1", agent_type="daedalus:builder"))

    def test_terminal_walkthrough_needs_a_person(self):
        r = subprocess.run([PY, DD, "try", "--ask"], cwd=self.p.root, input="y\n\n", capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("run it yourself in a terminal", r.stderr)
        self.assertEqual(self.state(), {})

    def test_no_nested_agent_sessions(self):
        for cmd in ('claude -p "/daedalus:verify UI-1 ok"', "cd x && codex exec 'verified'",
                    "npx @openai/codex exec hi", "C:/tools/claude.exe -p hi"):
            self.assertEqual(decision(self.p.hook("pre-tool", as_agent(None, "Bash", command=cmd))), "deny", cmd)
        for cmd in ("claude --version", 'git log --grep "fix the codex adapter"', "ls ~/.codex"):
            self.assertNotEqual(decision(self.p.hook("pre-tool", as_agent(None, "Bash", command=cmd))), "deny", cmd)


class Detect(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dd-detect-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, rel, text):
        fp = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)

    def test_empty_folder(self):
        d = dd.detect(self.root)
        self.assertEqual(d["gates"], [])
        self.assertTrue(any("no code yet" in n for n in d["notes"]))
        self.assertIn(".env", d["protected"])

    def test_node_web_app(self):
        self.write("package.json", json.dumps({"scripts": {"test": "vitest run", "lint": "eslint ."},
                                               "dependencies": {"next": "15"}, "devDependencies": {"typescript": "5"}}))
        self.write("pnpm-lock.yaml", "")
        self.write("tsconfig.json", "{}")
        self.write("playwright.config.ts", "export default { testDir: './e2e' }")
        self.write("app/page.tsx", "")
        self.write("node_modules/x/index.js", "")
        d = dd.detect(self.root)
        runs = {g["name"]: g for g in d["gates"]}
        self.assertEqual(d["kind"], "web app")
        self.assertEqual(runs["tests"]["run"], "pnpm run test")
        self.assertEqual(runs["lint"]["run"], "pnpm run lint")
        self.assertEqual(runs["types"]["run"], "npx tsc --noEmit")
        self.assertEqual(runs["e2e"]["evidence"], "e2e")
        self.assertEqual(runs["e2e"]["tests"], ["e2e/**"])
        self.assertIn("app/**", d["roles"]["source"])

    def test_python_uv_cli(self):
        self.write("pyproject.toml", '[project]\nname="calc"\ndependencies=[]\n[project.scripts]\ncalc="calc:main"\n'
                                     '[dependency-groups]\ndev=["pytest", "ruff"]\n')
        self.write("uv.lock", "")
        self.write("calc/__init__.py", "")
        self.write("tests/test_calc.py", "")
        d = dd.detect(self.root)
        runs = {g["name"]: g["run"] for g in d["gates"]}
        self.assertEqual(d["kind"], "command-line tool")
        self.assertEqual(runs, {"tests": "uv run pytest -q", "lint": "uv run ruff check ."})
        self.assertEqual(d["roles"]["source"], ["calc/**"])
        self.assertEqual(d["test_files"], 1)
        self.assertTrue(any("end-to-end" in n for n in d["notes"]))

    def test_init_auto(self):
        sh(self.root, "git", "init", "-q")
        self.write("go.mod", "module x\n")
        r = subprocess.run([PY, DD, "init", "--auto", "--name", "X"], cwd=self.root, capture_output=True, text=True,
                           env={**os.environ, "CLAUDE_PROJECT_DIR": self.root})
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = dd.load_config(self.root)
        self.assertEqual([g["run"] for g in cfg["gates"]], ["go test ./...", "go vet ./..."])
        self.assertIn(".env", cfg["protected"])
        self.assertEqual(dd.load_json(os.path.join(self.root, ".claude", "settings.json"), {})["env"],
                         {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "65"})


def patch(*headers):
    body = "\n".join(f"*** {h}\n+x" for h in headers)
    return {"tool_name": "apply_patch", "tool_input": {"command": f"*** Begin Patch\n{body}\n*** End Patch"}}


def rollout(path, tokens, window=258_400):
    """A minimal Codex rollout: a token_count event for the latest model call."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": tokens * 9},
            "last_token_usage": {"input_tokens": tokens - 100, "output_tokens": 100, "total_tokens": tokens},
            "model_context_window": window}}}) + "\n")


class Codex(unittest.TestCase):
    """The same rules through Codex's hooks: apply_patch, its agent roles, its token log, no 'ask'."""

    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}], protected=[".env"],
                         roles={"source": ["src/**"], "tests": ["tests/**"]})

    def tearDown(self):
        self.p.cleanup()

    def hook(self, event, payload, **kw):
        return self.p.hook(event, {"session_id": "c1", **payload}, host="codex", **kw)

    def test_patches_are_checked_file_by_file(self):
        self.assertIsNone(self.hook("pre-tool", patch("Add File: notes.md")))
        out = self.hook("pre-tool", patch("Update File: README.md", "Add File: .env"))
        self.assertEqual(decision(out), "deny")
        self.assertIn(".env is protected", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(decision(self.hook("pre-tool", patch("Update File: README.md", "Move to: .daedalus/gates/x"))),
                         "deny")
        bad = {"tool_name": "apply_patch", "tool_input": {"command": "garbage"}}
        self.assertEqual(decision(self.hook("pre-tool", bad)), "deny")

    def test_codex_agent_roles(self):
        builder = {"agent_id": "t1", "agent_type": "daedalus-builder"}
        self.assertEqual(decision(self.hook("pre-tool", {**patch("Add File: tests/test_x.py"), **builder})), "deny")
        self.assertIsNone(self.hook("pre-tool", {**patch("Add File: src/x.py"), **builder}))
        writer = {"agent_id": "t2", "agent_type": "daedalus-test-writer"}
        self.assertEqual(decision(self.hook("pre-tool", {**patch("Add File: src/x.py"), **writer})), "deny")
        # the orchestrator doesn't edit source; Claude's role names mean nothing here
        self.assertEqual(decision(self.hook("pre-tool", patch("Add File: src/x.py"))), "deny")
        self.assertEqual(decision(self.hook("pre-tool", {**patch("Add File: src/x.py"), "agent_id": "t3",
                                                         "agent_type": "daedalus:builder"})), "deny")
        spawn = {"tool_name": "collaborationspawn_agent", "tool_input": {"message": "gAAAA"}}
        self.assertEqual(decision(self.hook("pre-tool", {**spawn, **builder})), "deny")
        self.assertIsNone(self.hook("pre-tool", spawn))

    def test_ask_becomes_a_question_the_user_answers(self):
        push = bash("git push origin main")
        out = self.hook("pre-tool", push)
        self.assertEqual(decision(out), "deny")  # never "ask": Codex would treat it as a failed hook
        self.assertIn("Ask the user now", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(self.hook("user-prompt", {"prompt": "what's the weather"}))
        self.assertEqual(decision(self.hook("pre-tool", push)), "deny")
        note = self.hook("user-prompt", {"prompt": "yes, push it"})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("approved", note)
        self.assertIsNone(self.hook("pre-tool", bash("git push origin other")) and None)
        self.assertIsNone(self.hook("pre-tool", push))  # allowed once
        self.assertEqual(decision(self.hook("pre-tool", push)), "deny")  # and only once
        self.hook("user-prompt", {"prompt": "no"})
        self.assertEqual(decision(self.hook("pre-tool", push)), "deny")

    def test_a_subagents_prompt_never_approves(self):
        self.hook("pre-tool", bash("git push"))
        self.assertIsNone(self.hook("user-prompt", {"prompt": "yes", "agent_id": "t1", "agent_type": "daedalus-builder"}))
        self.assertEqual(decision(self.hook("pre-tool", bash("git push"))), "deny")

    def test_host_settings_are_guarded(self):
        self.assertEqual(decision(self.hook("pre-tool", patch("Update File: .codex/hooks.json"))), "deny")
        self.assertEqual(decision(self.p.hook("pre-tool", as_agent(None, "Edit", file_path=os.path.join(
            self.p.root, ".claude", "settings.json")))), "ask")
        self.assertEqual(decision(self.p.hook("pre-tool", as_agent("builder", "Write", file_path=os.path.join(
            self.p.root, ".codex", "config.toml")))), "deny")

    def test_typed_verify(self):
        self.p.write(".daedalus/specs/signup.json", json.dumps(TRY_SPEC))
        note = self.hook("user-prompt", {"prompt": "$daedalus:verify UI-1 ok fits"})["hookSpecificOutput"]
        self.assertIn("recorded UI-1 as verified", note["additionalContext"])
        state = dd.load_json(dd.requirements_path(self.p.root), {})
        self.assertEqual(state["UI-1"]["by"], "user (typed $daedalus:verify)")
        # in Claude Code the same prompt records nothing: there only the typed slash command counts
        self.p.hook("user-prompt", {"prompt": "$daedalus:verify UI-3 ok"})
        self.assertNotIn("UI-3", dd.load_json(dd.requirements_path(self.p.root), {}))

    def test_context_from_the_rollout(self):
        t = os.path.join(self.p.root, "rollout.jsonl")
        rollout(t, 145_000)  # 56 % of 258,400
        out = self.hook("pre-tool", {"transcript_path": t, **bash("ls")})
        self.assertIn("compacts itself", out["hookSpecificOutput"]["additionalContext"])
        self.assertIn("$daedalus:save", out["hookSpecificOutput"]["additionalContext"])
        rollout(t, 197_000)  # 76 %
        self.assertEqual(decision(self.hook("pre-tool", {"transcript_path": t, **bash("pytest -q")})), "deny")
        ctx = self.hook("session-start", {"source": "startup"})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("daedalus-builder", ctx)
        self.assertIn("codex", dd.load_json(os.path.join(self.p.root, ".daedalus", "hosts.json"), {}))

    def test_setup_writes_codex_project_files(self):
        cfg = dd.load_config(self.p.root)
        orig = dd.codex_window, dd.codex_wanted
        dd.codex_window, dd.codex_wanted = (lambda: ("gpt-test", 200_000)), (lambda root: True)
        try:
            self.p.write(".codex/config.toml", 'model_reasoning_effort = "high"\n\n[features]\nhooks = true\n')
            self.p.write("AGENTS.md", "# Rules\nBe nice.\n")
            dd.setup_codex_project(self.p.root, cfg)
            dd.setup_codex_project(self.p.root, cfg)  # idempotent
        finally:
            dd.codex_window, dd.codex_wanted = orig
        toml = dd.read(os.path.join(self.p.root, ".codex", "config.toml"))
        self.assertEqual(dd.toml_top_value(toml, "model_auto_compact_token_limit"), "123500")  # 65 % of 95 % of 200k
        self.assertEqual(dd.toml_top_value(toml, "model_post_turn_compact_threshold_percent"), "65")
        self.assertLess(toml.index("model_auto_compact_token_limit"), toml.index("[features]"))
        self.assertIn('model_reasoning_effort = "high"', toml)
        agent = dd.read(os.path.join(self.p.root, ".codex", "agents", "daedalus-builder.toml"))
        self.assertIn('name = "daedalus-builder"', agent)
        self.assertIn("Daedalus builder", agent)
        md = dd.read(os.path.join(self.p.root, "AGENTS.md"))
        self.assertTrue(md.startswith("# Rules"))
        self.assertEqual(md.count("## Daedalus harness"), 1)
        if sys.version_info >= (3, 11):
            import tomllib
            tomllib.loads(toml)
            tomllib.loads(agent)


class SubagentContext(unittest.TestCase):
    def test_subagent_measured_on_its_own_window(self):
        p = Project(context=PAUSE)
        try:
            main = os.path.join(p.root, "main.jsonl")
            transcript(main, 10_000)  # the main session is nearly empty
            sub = os.path.join(p.root, "main", "subagents", "agent-a1.jsonl")
            os.makedirs(os.path.dirname(sub))
            with open(sub, "w", encoding="utf-8") as fh:  # a subagent transcript: all its own (sidechain) calls
                fh.write(json.dumps({"type": "assistant", "isSidechain": True, "message": {
                    "model": "claude-sonnet-4-6", "usage": {"input_tokens": 10, "cache_read_input_tokens": 139_990,
                                                            "cache_creation_input_tokens": 0}}}) + "\n")
            # this subagent is at 70 % of 200k
            out = p.hook("pre-tool", {"transcript_path": main, "agent_id": "a1", "agent_type": "daedalus:builder",
                                      "tool_name": "Read", "tool_input": {"file_path": os.path.join(p.root, "README.md")}})
            self.assertEqual(decision(out), "deny")
            self.assertIn("report back", out["hookSpecificOutput"]["permissionDecisionReason"])
        finally:
            p.cleanup()


if __name__ == "__main__":
    unittest.main()
