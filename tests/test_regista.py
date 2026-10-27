"""End-to-end tests: each hook is driven the way Claude Code drives it (JSON on stdin), against a
throwaway git repo. Run: python -m unittest discover -s tests -v"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

# Never read this machine's home folder: its ~/.claude/settings.json may enable the old plugin, and the warning
# about that would show up in unrelated hook answers. Every tool run these tests start inherits this home.
TEST_HOME = tempfile.mkdtemp(prefix="regista-home-")
atexit.register(shutil.rmtree, TEST_HOME, True)
os.environ.update(HOME=TEST_HOME, CODEX_HOME=os.path.join(TEST_HOME, ".codex"))

TOOL = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts", "regista.py"))
sys.path.insert(0, os.path.dirname(TOOL))
import regista  # noqa: E402

PY = sys.executable


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True).stdout


class Project:
    def __init__(self, gates=None, protected=None, **cfg):
        self.root = tempfile.mkdtemp(prefix="regista-test-")
        sh(self.root, "git", "init", "-q", "-b", "main")
        sh(self.root, "git", "config", "user.email", "t@example.com")
        sh(self.root, "git", "config", "user.name", "T")
        env = {**os.environ, "CLAUDE_PROJECT_DIR": self.root}
        subprocess.run([PY, TOOL, "init", "--name", "Demo"], cwd=self.root, env=env, check=True, capture_output=True)
        path = os.path.join(self.root, ".regista", "config.json")
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
        cwd = data["cwd"] if os.path.isdir(str(data["cwd"])) else self.root  # never the repository itself
        r = subprocess.run([PY, TOOL, "hook", event, *(["--host", host] if host else [])], input=json.dumps(data),
                           capture_output=True, text=True, encoding="utf-8", env=env, cwd=cwd)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def cli(self, *args):
        return subprocess.run([PY, TOOL, *args], cwd=self.root, capture_output=True, text=True, encoding="utf-8")

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


def decision(out):
    return (out or {}).get("hookSpecificOutput", {}).get("permissionDecision")


def clean_env(**extra):
    """A subprocess environment without CLAUDE_PROJECT_DIR or any harness variable (new or old name), with the
    test home folder instead of the real one, plus extra."""
    env = {k: v for k, v in os.environ.items()
           if k != "CLAUDE_PROJECT_DIR" and not k.startswith(("REGISTA_", "DAEDALUS_"))}
    env.update(HOME=TEST_HOME, CODEX_HOME=os.path.join(TEST_HOME, ".codex"))
    env.update(extra)
    return env


def clean_environ(**extra):
    """This process's environment cleaned the same way, so helpers that copy os.environ start clean too."""
    return mock.patch.dict(os.environ, clean_env(**extra), clear=True)


def env_gate(out):
    """A gate that writes the worker and slot it sees, under the new and the old names, to the file out."""
    return {"name": "env", "run": 'printf "W=%s S=%s OW=%s OS=%s" "${REGISTA_WORKER-unset}" "${REGISTA_SLOT-unset}" '
                                  f'"${{DAEDALUS_WORKER-unset}}" "${{DAEDALUS_SLOT-unset}}" > "{out}"'}


class Globs(unittest.TestCase):
    def test_patterns(self):
        self.assertTrue(regista.matches("accuracy/truth/a.json", ["accuracy/truth/**"]))
        self.assertTrue(regista.matches("accuracy/truth/x/y.json", ["accuracy/truth/"]))
        self.assertFalse(regista.matches("accuracy/results/a.json", ["accuracy/truth/**"]))
        self.assertTrue(regista.matches("deep/dir/.env", [".env"]))
        self.assertTrue(regista.matches(".env", [".env"]))  # a root dotfile keeps its dot
        self.assertTrue(regista.matches("./.env", [".env"]))
        self.assertFalse(regista.matches(".env", ["env"]))
        self.assertTrue(regista.matches("src/geo/a.py", ["src/**/*.py"]))
        self.assertFalse(regista.matches("src/geo/a.txt", ["src/**/*.py"]))
        self.assertTrue(regista.matches("eval/labels/1.csv", ["eval/labels/*"]))

    def test_last_json(self):
        self.assertEqual(regista.last_json('noise\n{"a": 1}\n'), {"a": 1})
        self.assertEqual(regista.last_json('x\n{"a": 2, "b": 3}'), {"a": 2, "b": 3})
        self.assertIsNone(regista.last_json("no json here"))


class Hooks(unittest.TestCase):
    def tearDown(self):
        if getattr(self, "p", None):
            self.p.cleanup()

    def test_outside_project_is_silent(self):
        empty = tempfile.mkdtemp()
        try:
            r = subprocess.run([PY, TOOL, "hook", "pre-bash"], input=json.dumps({"cwd": empty, **bash("git commit -m x")}),
                               capture_output=True, text=True, cwd=empty, env={k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"})
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

    def test_push_asks(self):  # covers: CODE-3
        self.p = Project()
        out = self.p.hook("pre-bash", bash("git push origin main"))
        self.assertEqual(decision(out), "ask")
        self.assertTrue(out["hookSpecificOutput"]["permissionDecisionReason"].startswith("Regista: "), out)

    def test_push_block(self):
        self.p = Project(push="block")
        self.assertEqual(decision(self.p.hook("pre-bash", bash("git push"))), "deny")

    def test_powershell_tool_same_rules(self):
        self.p = Project(gates=[{"name": "tests", "run": "exit 1"}])
        out = self.p.hook("pre-bash", {"tool_name": "PowerShell", "tool_input": {"command": "git commit -m x"}})
        self.assertEqual(decision(out), "deny")

    def test_protected_edit_denied(self):  # covers: CODE-3
        self.p = Project(protected=["accuracy/truth/**"])
        path = os.path.join(self.p.root, "accuracy", "truth", "a.json")
        out = self.p.hook("pre-edit", {"tool_name": "Write", "tool_input": {"file_path": path}})
        self.assertEqual(decision(out), "deny")
        self.assertTrue(out["hookSpecificOutput"]["permissionDecisionReason"].startswith("Regista: "), out)
        ok = os.path.join(self.p.root, "src", "a.py")
        self.assertIsNone(self.p.hook("pre-edit", {"tool_name": "Edit", "tool_input": {"file_path": ok}}))

    def test_protected_shell_write_denied(self):
        self.p = Project(protected=["accuracy/truth/**"])
        self.assertEqual(decision(self.p.hook("pre-bash", bash("rm -rf accuracy/truth"))), "deny")
        self.assertEqual(decision(self.p.hook("pre-bash", bash("echo 1 > accuracy/truth/x.json"))), "deny")
        self.assertIsNone(self.p.hook("pre-bash", bash("cat accuracy/truth/x.json")))

    def test_baseline_edit_denied(self):
        self.p = Project()
        path = os.path.join(self.p.root, ".regista", "gates", "baseline.json")
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
        self.p.write(".regista/state/2026-01-01-first.md", "# h\n## Resume point\nstart\n")
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
        self.p.write(".regista/state/2026-01-02-save.md", "# h\n## Resume point\nnext\n")
        self.assertIsNone(self.p.hook("stop", {}))
        time.sleep(1.1)
        self.p.commit(".regista/status.md", "updated", "docs: save")  # committing the save itself
        self.assertIsNone(self.p.hook("stop", {}))

    def test_stop_remind_mode(self):
        self.p = Project(stop="remind")
        self.p.commit("src/a.py", "x = 1")
        out = self.p.hook("stop", {})
        self.assertNotIn("decision", out)
        self.assertIn("systemMessage", out)

    def test_session_start_context(self):  # covers: CODE-3
        self.p = Project()
        self.p.write(".regista/state/2026-01-03-x.md", "# h\n## Resume point\nWire the exporter.\n## Blocked on\n- nothing\n")
        out = self.p.hook("session-start", {"source": "startup"})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Wire the exporter.", ctx)
        self.assertIn("regista.py", ctx)
        self.assertIn("runs on the Regista harness", ctx)
        self.assertIn("Regista CLI", ctx)
        self.assertNotIn("Daedalus", ctx)
        self.assertNotIn("/daedalus:", ctx)
        self.assertNotIn("dd.py", ctx)
        self.assertLess(len(ctx), 10000)
        out = self.p.hook("session-start", {"source": "compact"})
        self.assertIn("compacted", out["hookSpecificOutput"]["additionalContext"])

    def test_snapshot_at_session_end(self):
        self.p = Project()
        self.p.write(".regista/state/2026-01-04-x.md", "# h\n")
        time.sleep(1.1)
        self.p.commit("src/b.py", "y = 2", "feat: b")
        self.p.hook("session-end", {"reason": "exit"})
        autos = [f for f in os.listdir(os.path.join(self.p.root, ".regista", "state")) if "-auto-" in f]
        self.assertEqual(len(autos), 1)
        self.p.hook("session-end", {"reason": "exit"})  # nothing new: no second snapshot
        autos = [f for f in os.listdir(os.path.join(self.p.root, ".regista", "state")) if "-auto-" in f]
        self.assertEqual(len(autos), 1)

    def test_state_readme_is_not_a_handoff(self):
        self.p = Project()
        self.assertIsNone(regista.newest_handoff(self.p.root, regista.load_config(self.p.root)))


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
        cfg = regista.load_config(self.p.root)
        self.assertEqual(regista.context_window(cfg, 1000, "claude-opus-5-5"), 1_000_000)
        self.assertEqual(regista.context_window(cfg, 1000, "claude-sonnet-5-5"), 1_000_000)
        self.assertEqual(regista.context_window(cfg, 1000, "claude-fable-5-1"), 1_000_000)
        self.assertEqual(regista.context_window(cfg, 1000, "claude-opus-4-7"), 1_000_000)
        self.assertEqual(regista.context_window(cfg, 1000, "claude-opus-4-6"), 200_000)
        self.assertEqual(regista.context_window(cfg, 1000, "claude-opus-4-6[1m]"), 1_000_000)
        self.assertEqual(regista.context_window(cfg, 250_000, "unknown-model"), 1_000_000)
        cfg["context"]["window"] = 400_000
        self.assertEqual(regista.context_window(cfg, 1000, "claude-opus-5-5"), 400_000)

    def test_tokens_ignore_output_and_subagents(self):
        transcript(self.t, 120_000)
        self.assertEqual(regista.context_tokens(self.t), (120_000, "claude-sonnet-4-6"))

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
        self.assertIsNone(self.tool(over, "Skill", skill="regista:pause"))
        self.assertIsNone(self.tool(over, "Write", file_path=os.path.join(self.p.root, ".regista", "state", "2026-10-09-pause.md")))
        self.assertIsNone(self.tool(over, "Edit", file_path=os.path.join(self.p.root, ".regista", "status.md")))
        self.assertIsNone(self.tool(over, "Bash", command="git status --short && git add -A && git commit -m wip"))
        self.assertIsNone(self.tool(over, "Bash", command=f'python "{TOOL}" gate'))
        self.assertEqual(decision(self.tool(over, "Bash", command="git status && rm -rf src")), "deny")

    def test_gates_still_apply_while_paused(self):
        self.p = Project(gates=[{"name": "tests", "run": "exit 1"}], context=PAUSE)
        self.assertEqual(decision(self.tool(132_000, "Bash", command="git commit -am wip")), "deny")

    def test_stop_blocked_until_pause_handoff_written(self):  # covers: CODE-3
        self.tool(132_000, "Read", file_path="x")  # crossing the limit records the moment
        out = self.call("stop", 132_000)
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("/regista:pause", out["reason"])
        time.sleep(1.1)
        self.p.write(".regista/state/2026-10-09-pause-x.md", "# h\n## Resume prompt\n/regista:start continue\n")
        self.assertIsNone(self.call("stop", 132_000))

    def test_stop_hook_active_never_loops(self):
        self.assertIsNone(self.call("stop", 132_000, stop_hook_active=True))

    def test_user_prompt_over_limit(self):  # covers: CODE-3
        out = self.call("user-prompt", 140_000, prompt="next feature please")
        self.assertIn("/regista:pause", out["hookSpecificOutput"]["additionalContext"])

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
        self.assertIn("/regista:save", note)
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
        rows = regista.read(os.path.join(self.p.root, ".regista", "compactions.jsonl")).strip().splitlines()
        self.assertEqual(json.loads(rows[-1])["trigger"], "auto")

    def test_subagent_backstop_too(self):
        sub = {"agent_id": "a1", "agent_type": "regista:builder", "tool_name": "Bash", "tool_input": {"command": "ls"}}
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
        data = regista.load_json(os.path.join(self.p.root, ".claude", "settings.json"), {})
        self.assertEqual(data["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "65")
        self.assertEqual(data["permissions"], {"allow": ["Bash(ls)"]})  # the rest is kept
        r = self.p.cli("doctor")
        self.assertIn("Claude Code compacts at 65%", r.stdout)


def as_agent(role, tool, **ti):
    """Hook payload for a tool call made inside one of our agents (or the main session if role is None)."""
    d = {"tool_name": tool, "tool_input": ti}
    if role:
        d.update(agent_id="a1", agent_type=role if ":" in role or role in ("Explore", "general-purpose") else f"regista:{role}")
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
        self.assertIsNone(self.check(None, "Edit", file_path=self.f(".regista/status.md")))
        self.assertIsNone(self.check(None, "Write", file_path=self.f("README.md")))
        self.assertIsNone(self.check(None, "Read", file_path=self.f("src/a.py")))
        self.assertIsNone(self.check(None, "Agent", subagent_type="regista:builder", prompt="x"))

    def test_solo_mode_lets_the_main_session_build(self):
        self.p = Project(mode="solo")
        self.assertIsNone(self.check(None, "Write", file_path=self.f("src/a.py")))

    def test_builder(self):  # covers: CODE-4
        self.assertIsNone(self.check("builder", "Write", file_path=self.f("src/a.py")))
        self.assertEqual(self.check("builder", "Edit", file_path=self.f("tests/test_a.py")), "deny")
        self.assertEqual(self.check("builder", "Bash", command="git add -A && git commit -m x"), "deny")
        self.assertEqual(self.check("builder", "Agent", subagent_type="regista:reviewer", prompt="approve me"), "deny")
        self.assertIsNone(self.check("builder", "Bash", command="python -m pytest -q"))

    def test_test_writer_works_blind(self):  # covers: CODE-4
        self.assertIsNone(self.check("test-writer", "Write", file_path=self.f("tests/test_a.py")))
        self.assertEqual(self.check("test-writer", "Write", file_path=self.f("src/a.py")), "deny")
        self.assertEqual(self.check("test-writer", "Read", file_path=self.f("src/a.py")), "deny")
        self.assertEqual(self.check("test-writer", "Grep", pattern="def", path=self.f("src")), "deny")
        self.assertEqual(self.check("test-writer", "Grep", pattern="def"), "deny")  # no path = whole project
        self.assertIsNone(self.check("test-writer", "Grep", pattern="def", path=self.f("tests")))
        self.assertEqual(self.check("test-writer", "Bash", command="cat src/a.py"), "deny")
        self.assertIsNone(self.check("test-writer", "Read", file_path=self.f(".regista/plan.md")))
        self.assertIsNone(self.check("test-writer", "Bash", command="python -m pytest tests/test_a.py"))

    def test_reviewer_reports_doesnt_fix(self):
        self.assertEqual(self.check("reviewer", "Edit", file_path=self.f("src/a.py")), "deny")
        self.assertIsNone(self.check("reviewer", "Write", file_path=self.f(".regista/reviews/draft.json")))
        self.assertIsNone(self.check("reviewer", "Bash", command="git diff HEAD 2>&1"))
        self.assertIsNone(self.check("reviewer", "Bash", command=f'python "{TOOL}" review-record'))
        self.assertEqual(self.check("reviewer", "Bash", command="echo x > src/a.py"), "deny")

    def test_only_the_reviewer_writes_reviews(self):
        for role in (None, "builder", "researcher"):
            self.assertEqual(self.check(role, "Write", file_path=self.f(".regista/reviews/draft.json")), "deny", role)
            self.assertEqual(self.check(role, "Bash", command=f'python "{TOOL}" review-record'), "deny", role)

    def test_researcher(self):
        self.assertIsNone(self.check("researcher", "Write", file_path=self.f(".regista/research/q1.md")))
        self.assertEqual(self.check("researcher", "Write", file_path=self.f("src/a.py")), "deny")
        self.assertIsNone(self.check("researcher", "WebFetch", url="https://example.com"))

    def test_other_subagents_cant_edit_code_in_orchestrated_mode(self):
        self.assertEqual(self.check("general-purpose", "Write", file_path=self.f("src/a.py")), "deny")
        self.assertIsNone(self.check("Explore", "Read", file_path=self.f("src/a.py")))

    def test_spoofed_role_name_is_just_other(self):  # covers: CODE-4
        self.assertEqual(regista.role_of({"agent_id": "x", "agent_type": "reviewer"}), "other")
        self.assertEqual(regista.role_of({"agent_id": "x", "agent_type": "regista:reviewer"}), "reviewer")
        self.assertEqual(regista.role_of({}), "main")

    def test_subagent_start_injects_role(self):  # covers: CODE-4, CODE-3
        out = self.p.hook("subagent-start", {"agent_id": "a9", "agent_type": "regista:test-writer"})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("test-writer", ctx)
        self.assertIn("regista.py", ctx)
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
        self.p.write(".regista/reviews/draft.json", json.dumps(
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
        def reviews(cmd):
            return regista.under(self.p.root, regista.shell_effects(cmd, self.p.root)["writes"], [".regista/reviews/**"])
        self.assertEqual(reviews("sed -i 's/x/y/' .regista/plan.md && git add -A .regista/reviews/a.json && "
                                 "git commit -m x"), [])
        self.assertEqual(reviews("git status && cp a.json .regista/reviews/b.json"), [".regista/reviews/b.json"])
        self.assertIsNone(self.p.hook("pre-tool", as_agent(None, "Bash", command=(
            "sed -i 's/x/y/' .regista/plan.md && git add -A .regista/reviews/a.json && git commit -m x"))))

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
        self.p.hook("subagent-start", {"agent_id": "r1", "agent_type": "regista:reviewer"})
        out = self.p.hook("subagent-stop", {"agent_id": "r1", "agent_type": "regista:reviewer"})
        self.assertEqual(out.get("decision"), "block")
        self.assertIsNone(self.p.hook("subagent-stop", {"agent_id": "r1", "agent_type": "regista:reviewer",
                                                       "stop_hook_active": True}))
        self.p.write("src/a.py", BIG)
        time.sleep(1.1)
        self.draft()
        self.assertEqual(self.p.cli("review-record").returncode, 0)
        self.assertIsNone(self.p.hook("subagent-stop", {"agent_id": "r1", "agent_type": "regista:reviewer"}))


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

    def test_another_copy_of_the_harness_isnt_a_scratch_probe(self):  # covers: CODE-7
        with open(TOOL, encoding="utf-8") as f:
            src = f.read()
        marker = src[src.index('"""'):].splitlines()[0]  # the opening line of the tool's docstring
        copy = marker + ("\n" if marker.count('"""') > 1 else '\n"""\n') + "import sys\nprint(sys.argv)\n"
        scratch = tempfile.mkdtemp(prefix="regista-scratch-")

        def probe(name, text):
            path = os.path.join(scratch, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return regista.agent_script(path, self.p.root)

        try:
            self.assertFalse(probe("regista.py", copy))
            self.assertFalse(probe("dd.py", copy))
            self.assertFalse(probe("dd.py", '"""Daedalus core: hooks and commands.\n"""\nimport sys\n'))
            self.assertTrue(probe("probe.py", "print('hello')\n"))
            self.assertTrue(probe("regista.py", "print('hello')\n"))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_builder_cant_edit_test_config(self):
        for path in ("pytest.ini", "src/conftest.py", "web/vitest.config.ts", "src/__snapshots__/a.snap"):
            self.assertEqual(self.check("builder", "Write", file_path=os.path.join(self.p.root, path)), "deny", path)

    def test_test_writer_owns_test_config(self):
        self.assertIsNone(self.check("test-writer", "Write", file_path=os.path.join(self.p.root, "tests", "conftest.py")))

    def test_harness_files_belong_to_the_harness(self):
        cfgfile = os.path.join(self.p.root, ".regista", "config.json")
        self.assertEqual(self.check(None, "Edit", file_path=cfgfile), "ask")  # the user decides
        self.assertEqual(self.check("builder", "Edit", file_path=cfgfile), "deny")
        self.assertEqual(self.check(None, "Bash", command="sed -i 's/65/95/' .regista/config.json"), "ask")
        for role in (None, "builder", "reviewer"):
            self.assertEqual(self.check(role, "Write", file_path=os.path.join(
                self.p.root, ".regista", "gates", "requirements.json")), "deny", role)
        self.assertEqual(self.check(None, "Bash", command="echo {} > .regista/gates/baseline.json"), "deny")
        self.assertEqual(self.check(None, "Bash", command=f'python "{TOOL}" verify-manual UI-1 --note ok'), "deny")
        self.assertIsNone(self.check(None, "Bash", command=f'python "{TOOL}" gate'))


class ShellReading(unittest.TestCase):
    """The guards judge what a command writes and reads, not the words in it. The allowed cases are
    refusals the FPL build actually hit (25-26 October 2026); the denied ones are the routes that
    must stay closed."""

    def setUp(self):
        self.p = Project(protected=[".env", ".env.*.local", "*.pem"])
        self.tmp = tempfile.mkdtemp(prefix="regista-scratch-")

    def tearDown(self):
        self.p.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def check(self, role, cmd, **extra):
        return decision(self.p.hook("pre-tool", {**as_agent(role, "Bash", command=cmd), **extra}))

    def test_fpl_false_alarms_are_allowed(self):
        t = self.tmp
        allowed = [
            ("researcher", 'node -e "console.log([1].map(x => x+1))"'),
            ("researcher", f"cd {t} && python3 -I -c \"\nimport json\nif len([1])>1: print('x')\n\""),
            ("researcher", "cat > .regista/research/prices.md <<'EOF'\n# Notes\nprices > 0 and rm -rf x\nEOF"),
            ("reviewer", f"S={t}; mkdir -p $S/shot && cat > $S/mk.mjs <<'EOF'\n"
                         f"import {{ writeFileSync }} from 'node:fs';\nimport {{ f }} from '{self.p.root}/src/a.js';\n"
                         "writeFileSync(process.argv[2], JSON.stringify(f()));\nEOF\nnode $S/mk.mjs $S/out.json"),
            ("reviewer", f"cd {t} && node --input-type=module -e 'const a = [1]; console.log(a.length > 0)'"),
            ("reviewer", f'D=$(mktemp -d) && echo hi > "$D/probe.txt"'),
            ("builder", "python3 - <<'EOF'\np='src/poll.js'\ns=open(p).read()\n"
                        "s=s.replace('a','buildMonthSnapshots(db.snapshot)')\nopen(p,'w').write(s)\nEOF"),
            ("builder", "grep -n 'assert' tests/test_a.py; sed -n 1,20p tests/test_a.py"),
            ("builder", f"node --test tests/e2e/fonts.test.js > {t}/out.txt 2>&1; tail -5 {t}/out.txt"),
            ("test-writer", "node --test tests/unit/a.test.js 2>&1 | head; ls src/core"),
            ("test-writer", 'pgrep -af "src/api/main.js" | head'),
            ("test-writer", "python3 - <<'EOF'\np='tests/unit/w.test.js'\ns=open(p).read()\n"
                            "s=s.replace(\"from '../../src/core/whatif.js'\", \"from '../../src/core/w2.js'\")\n"
                            "open(p,'w').write(s)\nEOF"),
            ("test-writer", f"M={t}/mirror; mkdir -p $M/src && cp -r tests $M/ && cat > $M/src/ref.js <<'EOF'\n"
                            "export const f = () => 1;\nEOF"),
            (None, "cat > deploy/systemd/fpl.service <<'EOF'\nEnvironmentFile=/etc/fpl/fpl.env\nEOF\n"
                   "cat > deploy/fpl.env.example <<'EOF'\nDATABASE_URL=x\nEOF"),
            (None, f"cat > {t}/list.py <<'EOF'\ncp = env_file('/x/.env')\nprint(cp)\nEOF\npython3 -I {t}/list.py"),
            (None, "grep -n 'ownership' docs/spec.md | head -3 && git add -A && git commit -qm 'docs: spec -n'"),
            (None, "python3 - <<'EOF'\np='.regista/status.md'; t=open(p).read()\n"
                   "t+='record it with verify-manual'\nopen(p,'w').write(t)\nEOF"),
        ]
        for role, cmd in allowed:
            self.assertNotEqual(self.check(role, cmd), "deny", f"{role}: {cmd}")
        self.assertIsNone(decision(self.p.hook("pre-tool", as_agent("reviewer", "Write", file_path=f"{t}/probe.mjs"))))
        self.assertIsNone(decision(self.p.hook("pre-tool", as_agent("researcher", "Write", file_path=f"{t}/b.py"))))

    def test_writes_by_any_route_are_still_caught(self):
        t = self.tmp
        denied = [
            ("builder", "python3 - <<'EOF'\nopen('tests/test_a.py', 'w').write('')\nEOF"),
            ("builder", "node -e \"require('fs').writeFileSync('tests/a.test.js', '')\""),
            ("builder", f"cat > {t}/x.py <<'EOF'\nopen('tests/test_a.py', 'w')\nEOF\npython3 {t}/x.py"),
            ("builder", "S=tests; echo x > $S/test_a.py"),
            ("builder", "cd tests && echo x > test_a.py"),
            ("builder", 'bash -c "rm tests/test_a.py"'),
            ("builder", "git checkout -- tests/test_a.py"),
            ("builder", "find tests -name '*.py' -delete"),
            ("builder", "perl -pi -e 's/a/b/' tests/test_a.py"),
            ("builder", "tee -a tests/test_a.py < /dev/null"),
            ("builder", "echo x | sudo tee tests/test_a.py"),
            ("builder", "cp {}/x.py tests/".format(t)),
            ("builder", "echo '[pytest]' >| pytest.ini"),
            ("reviewer", "node -e \"require('fs').writeFileSync('src/a.py', 'x')\""),
            ("reviewer", "sed -i 's/a/b/' src/a.py"),
            ("researcher", "echo x > notes.md"),
            ("test-writer", "cat src/a.py"),
            ("test-writer", "grep -rn 'def ' ."),
            ("test-writer", "python3 -c \"print(open('src/a.py').read())\""),
            ("test-writer", "cp src/a.py /tmp/a.py"),
            (None, "echo SECRET=1 >> .env"),
            (None, "rm -f certs/server.pem"),
            (None, "git commit -qn -m x"),
            (None, "git commit --no-verify -m x"),
        ]
        os.makedirs(os.path.join(self.p.root, "tests"), exist_ok=True)
        for role, cmd in denied:
            self.assertEqual(self.check(role, cmd), "deny", f"{role}: {cmd}")
        self.assertEqual(self.check(None, "python3 - <<'EOF'\nimport json\np='.regista/config.json'\n"
                                          "c=json.load(open(p)); c['gates']=[]\njson.dump(c, open(p, 'w'))\nEOF"), "ask")

    def test_protected_paths_outside_the_project(self):
        outside = os.path.join(self.tmp, "klotho")
        self.p = Project(protected=[outside])
        self.assertEqual(self.check(None, f"rm -rf {outside}/data"), "deny")
        self.assertEqual(decision(self.p.hook("pre-tool", as_agent(None, "Write", file_path=f"{outside}/a.txt"))), "deny")
        self.assertIsNone(self.check(None, f"cat {outside}/a.txt"))

    def test_nobody_writes_the_transcripts(self):
        tdir = os.path.join(self.tmp, "projects", "-demo")
        os.makedirs(tdir)
        extra = {"transcript_path": os.path.join(tdir, "s1.jsonl")}
        self.assertEqual(self.check(None, f"echo '{{}}' >> {tdir}/s1.jsonl", **extra), "deny")
        self.assertEqual(decision(self.p.hook("pre-tool", {**as_agent(None, "Write", file_path=f"{tdir}/s1.jsonl"),
                                                             **extra})), "deny")
        self.assertIsNone(self.check(None, f"echo hi > {self.tmp}/note.txt", **extra))


class GateRuns(unittest.TestCase):
    """Gates run once per state of the files, one run at a time, and the setup commit needs none."""

    def setUp(self):
        self.count = tempfile.mktemp(prefix="regista-count-")
        self.p = Project(gates=[{"name": "tests", "run": f"echo run >> {self.count}"}])

    def tearDown(self):
        self.p.cleanup()
        if os.path.exists(self.count):
            os.remove(self.count)

    def runs(self):
        return len(regista.read(self.count).splitlines())

    def commit(self):
        return self.p.hook("pre-tool", as_agent(None, "Bash", command="git add -A && git commit -m x"))

    def test_a_passing_run_is_reused_for_the_same_files(self):
        self.p.write("src/a.py", "x = 1\n")
        self.assertEqual(self.p.cli("gate").returncode, 0)
        self.assertIn("reused", self.p.cli("gate").stdout)
        self.p.write(".regista/status.md", "notes\n")  # the harness's own records don't void it
        self.assertIsNone(decision(self.commit()))
        self.assertEqual(self.runs(), 1)
        self.p.cli("gate", "--fresh")
        self.assertEqual(self.runs(), 2)
        self.p.write("src/a.py", "x = 2\n")
        self.assertIsNone(decision(self.commit()))
        self.assertEqual(self.runs(), 3)

    def test_one_gate_run_at_a_time(self):
        held = regista.gate_lock(self.p.root, 1)
        self.assertIsNotNone(held)
        self.assertIsNone(regista.gate_lock(self.p.root, 1))
        held.close()
        again = regista.gate_lock(self.p.root, 1)
        self.assertIsNotNone(again)
        again.close()

    def test_gates_see_the_worker_and_slot_under_both_names(self):  # covers: CODE-5
        with clean_environ():
            env = regista.gate_env(self.p.root)
        self.assertEqual((env["REGISTA_WORKER"], env["REGISTA_SLOT"]), ("", "0"))
        self.assertEqual((env["DAEDALUS_WORKER"], env["DAEDALUS_SLOT"]), ("", "0"))
        out = tempfile.mktemp(prefix="regista-env-")
        p = Project(gates=[env_gate(out)])
        try:
            r = subprocess.run([PY, TOOL, "gate", "--fresh"], cwd=p.root, capture_output=True, text=True,
                               encoding="utf-8", env=clean_env())
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            with open(out, encoding="utf-8") as f:
                self.assertEqual(f.read(), "W= S=0 OW= OS=0")
        finally:
            p.cleanup()
            if os.path.exists(out):
                os.remove(out)

    def test_setup_and_record_commits_skip_the_gates(self):
        fresh = tempfile.mkdtemp(prefix="regista-fresh-")
        try:
            sh(fresh, "git", "init", "-q", "-b", "main")
            subprocess.run([PY, TOOL, "init", "--name", "New"], cwd=fresh, check=True, capture_output=True,
                           env={**os.environ, "CLAUDE_PROJECT_DIR": fresh})
            cfgfile = os.path.join(fresh, ".regista", "config.json")
            c = regista.load_json(cfgfile, {})
            c["gates"] = [{"name": "tests", "run": "npm test"}]  # no package.json yet: it can't pass
            regista.save_json(cfgfile, c)
            r = subprocess.run([PY, TOOL, "hook", "pre-tool"], capture_output=True, text=True, cwd=fresh, input=json.dumps(
                {"cwd": fresh, **bash("git add -A && git commit -m 'chore: set up Regista'")}))
            self.assertEqual(r.stdout.strip(), "", r.stdout)
        finally:
            shutil.rmtree(fresh, ignore_errors=True)
        self.p = Project(gates=[{"name": "tests", "run": "exit 1"}], no_gates_for=["docs/**"])
        self.p.write(".regista/status.md", "done\n")
        self.p.write("docs/spec.md", "spec\n")
        self.assertIsNone(decision(self.commit()))
        self.p.write("src/a.py", "x = 1\n")
        self.assertEqual(decision(self.commit()), "deny")

    def test_loosening_the_config_needs_the_user(self):
        cfgfile = os.path.join(self.p.root, ".regista", "config.json")
        c = regista.load_json(cfgfile, {})
        c["gates"].append({"name": "lint", "run": "echo lint"})
        c["protected"] = ["data/**"]
        regista.save_json(cfgfile, c)
        self.assertIsNone(decision(self.commit()))  # adding checks is fine
        sh(self.p.root, "git", "add", "-A")
        sh(self.p.root, "git", "commit", "-q", "-m", "more checks")
        c["gates"] = c["gates"][:1]
        c["protected"] = []
        regista.save_json(cfgfile, c)
        out = self.commit()
        self.assertEqual(decision(out), "ask")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("'lint' was removed", reason)
        self.assertIn("data/**", reason)

    def test_requirement_results_describe_commits_and_dont_churn(self):
        self.p.write(".regista/specs/stats.json", json.dumps({**SPEC, "requirements": [SPEC["requirements"][0]]}))
        self.p.write("tests/test_stats.py", "# covers: STATS-1\n")
        self.assertIsNone(decision(self.commit()))
        state = regista.load_json(regista.requirements_path(self.p.root), {})
        self.assertTrue(state["STATS-1"]["passes"])
        sh(self.p.root, "git", "add", "-A")
        sh(self.p.root, "git", "commit", "-q", "-m", "stats")
        before = regista.read(regista.requirements_path(self.p.root))
        self.p.write("src/a.py", "half finished\n")
        out = self.p.cli("gate")
        self.assertIn("not updated", out.stdout)
        self.p.write("src/a.py", "x = 1\n")
        self.assertIsNone(decision(self.commit()))
        self.assertEqual(regista.read(regista.requirements_path(self.p.root)), before)  # same result: no rewrite


class FailClosed(unittest.TestCase):
    """Improvement 2: a broken guard blocks; it never waves the action through."""

    def setUp(self):
        self.p = Project()
        self.p.write(".regista/config.json", "{ not json")

    def tearDown(self):
        self.p.cleanup()

    def test_broken_config_blocks_tool_calls(self):
        out = self.p.hook("pre-tool", as_agent(None, "Bash", command="git commit -m x"))
        self.assertEqual(decision(out), "deny")
        self.assertIn("Failing closed", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_escape_hatch(self):
        self.assertIsNone(self.p.hook("pre-tool", as_agent(None, "Bash", command="ls"),
                                      extra_env={"REGISTA_FAIL_OPEN": "1"}))

    def test_escape_hatch_under_the_old_name(self):  # covers: CODE-6
        ls = as_agent(None, "Bash", command="ls")
        with clean_environ():
            self.assertEqual(decision(self.p.hook("pre-tool", ls)), "deny")
            self.assertIsNone(self.p.hook("pre-tool", ls, extra_env={"DAEDALUS_FAIL_OPEN": "1"}))

    def test_other_events_dont_trap_the_session(self):
        self.assertIsNone(self.p.hook("stop", {}))
        self.assertIsNone(self.p.hook("session-start", {"source": "startup"}))

    def test_gate_budget(self):
        self.p.write(".regista/config.json", "{}")
        gates = [{"name": "slow", "run": "sleep 3", "timeout": 60}, {"name": "next", "run": "echo ok"}]
        res = regista.run_gates(self.p.root, regista.load_config(self.p.root), gates, budget=2)
        self.assertEqual([r["status"] for r in res], ["fail", "fail"])  # cut at the budget, then no time left
        self.assertIn("no time left", res[1]["fails"][0])


class OldEnvNames(unittest.TestCase):
    """The harness's own variables are read as REGISTA_<x> first, then DAEDALUS_<x> (the old name)."""

    NAMES = ("BASH", "CONTEXT_WINDOW", "FAIL_OPEN", "HOST", "CLAUDE", "TMUX", "LAUNCH_WAIT")

    def test_prefixes(self):  # covers: CODE-6
        self.assertEqual(regista.ENV_PREFIX, "REGISTA_")
        self.assertEqual(regista.OLD_ENV_PREFIX, "DAEDALUS_")

    def test_new_name_first_then_the_old_one(self):  # covers: CODE-6
        for name in self.NAMES:
            new, old = "REGISTA_" + name, "DAEDALUS_" + name
            cases = [({}, "dflt"), ({new: "new"}, "new"), ({old: "old"}, "old"), ({new: "new", old: "old"}, "new"),
                     ({new: "", old: ""}, "dflt"), ({new: "", old: "old"}, "old"), ({new: "new", old: ""}, "new"),
                     ({new: ""}, "dflt"), ({old: ""}, "dflt")]
            for env, want in cases:
                with self.subTest(name=name, env=env), clean_environ(**env):
                    self.assertEqual(regista.env_value(name, "dflt"), want)

    def test_default_is_none(self):  # covers: CODE-6
        with clean_environ():
            for name in self.NAMES:
                self.assertIsNone(regista.env_value(name), name)


SPEC = {"step": "stats", "milestone": "M1", "title": "Statistics", "requirements": [
    {"id": "STATS-1", "text": "WHEN the list is empty mean SHALL raise ValueError", "verify": "test"},
    {"id": "STATS-2", "text": "WHEN run from the CLI the tool SHALL print the mean", "verify": "e2e"}]}
E2E_GATE = {"name": "e2e", "run": "echo ok", "when": ["commit"], "evidence": "e2e", "tests": ["e2e/**"]}


class Requirements(unittest.TestCase):
    """Improvements 3 and 4: requirements pass only through the gates; milestones need end-to-end proof."""

    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}, E2E_GATE])
        self.p.write(".regista/specs/stats.json", json.dumps(SPEC))

    def tearDown(self):
        self.p.cleanup()

    def commit(self):
        return self.p.hook("pre-tool", as_agent(None, "Bash", command="git add -A && git commit -m feat"))

    def state(self):
        return regista.load_json(regista.requirements_path(self.p.root), {})

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
        done, why = regista.milestone_status(self.p.root, regista.load_config(self.p.root), "M1")
        self.assertFalse(done)
        self.assertIsNone(self.commit())
        self.assertTrue(self.state()["STATS-1"]["passes"])
        self.assertTrue(self.state()["STATS-2"]["passes"])
        self.assertEqual(self.p.cli("milestone", "M1").returncode, 0)

    def test_a_failing_gate_fails_its_requirements(self):
        self.p = Project(gates=[{"name": "tests", "run": "exit 1"}, E2E_GATE])
        self.p.write(".regista/specs/stats.json", json.dumps(SPEC))
        self.p.write("tests/test_stats.py", "# covers: STATS-1\n")
        self.p.write("e2e/cli_test.sh", "# covers: STATS-2\n")
        self.assertEqual(decision(self.commit()), "deny")
        self.assertNotIn("STATS-1", self.state())  # a refused commit records nothing: results describe commits
        sh(self.p.root, "git", "add", "-A")
        sh(self.p.root, "git", "commit", "-q", "-m", "tests")
        self.assertNotEqual(self.p.cli("gate").returncode, 0)  # a run on a clean tree describes the commit
        self.assertFalse(self.state()["STATS-1"]["passes"])
        self.assertNotEqual(self.p.cli("milestone", "M1").returncode, 0)

    def test_milestone_needs_end_to_end_evidence(self):
        spec = {**SPEC, "requirements": [SPEC["requirements"][0]]}  # unit-tested only
        self.p.write(".regista/specs/stats.json", json.dumps(spec))
        self.p.write("tests/test_stats.py", "# covers: STATS-1\n")
        self.assertIsNone(self.commit())
        done, why = regista.milestone_status(self.p.root, regista.load_config(self.p.root), "M1")
        self.assertFalse(done)
        self.assertIn("end-to-end", " ".join(why))

    def test_planned_specs_dont_block(self):
        self.p.write(".regista/specs/stats.json", json.dumps({**SPEC, "state": "planned"}))
        self.assertIsNone(self.commit())

    def test_bad_specs_are_reported(self):
        bad = {**SPEC, "requirements": [{"id": "stats one", "text": "", "verify": "vibes"}, SPEC["requirements"][0]]}
        self.p.write(".regista/specs/stats.json", json.dumps(bad))
        self.p.write(".regista/specs/dup.json", json.dumps({**SPEC, "step": "dup"}))
        _, problems = regista.load_specs(self.p.root)
        text = " ".join(problems)
        self.assertIn("must look like NAME-1", text)
        self.assertIn("also defined", text)
        self.assertIn("verify must be", text)

    def test_id_matching_is_exact(self):
        self.p.write("tests/test_stats.py", "# covers: STATS-10, XSTATS-1\n")
        cov = regista.coverage(self.p.root, regista.load_config(self.p.root), [SPEC])
        self.assertEqual(cov["STATS-1"]["tests"], [])

    def test_manual_requirements_need_the_user(self):
        spec = {**SPEC, "requirements": [{"id": "UX-1", "text": "The page SHALL feel fast on a phone", "verify": "manual"}]}
        self.p.write(".regista/specs/ux.json", json.dumps({**spec, "milestone": "M2"}))
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


PLAN = """# Demo: plan

## M1 — Stats
Steps:
- [x] mean
- [ ] median

## M2 — CLI
Steps:
- [ ] calc command

## Out of scope
- [ ] charts
"""


class BuildAll(unittest.TestCase):
    """`/regista:build all`: the run keeps the session going between milestones while it makes progress."""

    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}])
        self.p.commit(".regista/plan.md", PLAN, "docs: plan")

    def tearDown(self):
        self.p.cleanup()

    def test_plan_milestones_in_order_with_open_steps(self):
        ms = regista.plan_milestones(self.p.root, regista.load_config(self.p.root))
        self.assertEqual([(m["id"], m["title"], m["open_steps"]) for m in ms], [("M1", "Stats", 1), ("M2", "CLI", 1)])

    def test_start_status_end(self):
        r = self.p.cli("run", "start")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("M1 — Stats: gate open", r.stdout)
        self.assertIn("active", self.p.cli("run").stdout)
        self.assertIn("Build-all run", self.p.cli("resume").stdout)
        self.assertNotEqual(self.p.cli("run", "end").returncode, 0)  # a reason is required
        r = self.p.cli("run", "end", "--reason", "M2 waits on a decision")
        self.assertIn("ended (stopped)", r.stdout)
        out = self.p.hook("stop", {}) or {}
        self.assertNotIn("build-all", out.get("reason", ""))  # only the usual save reminder is left

    def test_stop_sends_the_session_back_while_commits_keep_coming(self):
        self.p.cli("run", "start")
        out = self.p.hook("stop", {})
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("build-all run is active", out["reason"])
        self.assertIn("M2 — CLI", out["reason"])
        self.p.commit("src/a.py", "x = 1", "feat: median")
        out = self.p.hook("stop", {"stop_hook_active": True})  # nudges again after an earlier block
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("since the last handoff", out["reason"])

    def test_a_run_without_progress_ends_itself(self):
        self.p.cli("run", "start")
        self.assertEqual(self.p.hook("stop", {}).get("decision"), "block")
        out = self.p.hook("stop", {"stop_hook_active": True})
        self.assertNotIn("decision", out)
        self.assertIn("stopped", out["systemMessage"])
        self.assertIn("stalled", self.p.cli("run").stdout)
        self.assertIsNone(self.p.hook("stop", {"stop_hook_active": True}))

    def test_only_the_orchestrator_starts_or_ends_a_run(self):
        for cmd in (f'python "{TOOL}" run start', f'python "{TOOL}" run end --reason done'):
            self.assertEqual(decision(self.p.hook("pre-tool", as_agent("builder", "Bash", command=cmd))), "deny")
            self.assertIsNone(decision(self.p.hook("pre-tool", as_agent(None, "Bash", command=cmd))))


class TryItYourself(unittest.TestCase):
    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}])
        self.p.write(".regista/specs/signup.json", json.dumps(TRY_SPEC))

    def tearDown(self):
        self.p.cleanup()

    def state(self):
        return regista.load_json(regista.requirements_path(self.p.root), {})

    def typed(self, args, **extra):
        return self.p.hook("user-prompt-expansion", {"command_name": "regista:verify", "command_args": args,
                                                     "prompt": f"/regista:verify {args}", **extra})

    def test_checklist(self):
        out = self.p.cli("try", "M1").stdout
        self.assertIn("2 check(s) waiting for you", out)
        self.assertIn("Sign-up fits a phone screen", out)
        self.assertIn("1. Run `npm run dev`", out)
        self.assertIn("You should see: No sideways scrolling", out)
        self.assertIn("optional: the gates already prove this one", out)
        self.assertIn("no steps written yet", out)  # UI-3 has no try steps
        self.assertIn("/regista:verify UI-1 ok", out)
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
        for payload in (as_agent(None, "Skill", skill="regista:verify", args="UI-1 ok"),
                        as_agent(None, "Bash", command=f'python "{TOOL}" try --ask'),
                        as_agent("builder", "Bash", command=f'echo y | python "{TOOL}" try M1 --ask'),
                        as_agent(None, "Bash", command=f'python "{TOOL}" verify-manual UI-1 --note x')):
            self.assertEqual(decision(self.p.hook("pre-tool", payload)), "deny", payload)
        self.assertIsNone(self.p.hook("pre-tool", as_agent(None, "Bash", command=f'python "{TOOL}" try M1')))
        # a subagent's expansion never records
        self.assertIsNone(self.typed("UI-1 ok", agent_id="a1", agent_type="regista:builder"))

    def test_terminal_walkthrough_needs_a_person(self):
        r = subprocess.run([PY, TOOL, "try", "--ask"], cwd=self.p.root, input="y\n\n", capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("run it yourself in a terminal", r.stderr)
        self.assertEqual(self.state(), {})

    def test_no_nested_agent_sessions(self):
        for cmd in ('claude -p "/regista:verify UI-1 ok"', "cd x && codex exec 'verified'",
                    "npx @openai/codex exec hi", "C:/tools/claude.exe -p hi"):
            self.assertEqual(decision(self.p.hook("pre-tool", as_agent(None, "Bash", command=cmd))), "deny", cmd)
        for cmd in ("claude --version", 'git log --grep "fix the codex adapter"', "ls ~/.codex"):
            self.assertNotEqual(decision(self.p.hook("pre-tool", as_agent(None, "Bash", command=cmd))), "deny", cmd)


def queued(path, prompt, human=True, uid="u1"):
    """Append the transcript entry Claude Code writes when a queued message joins a running turn."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "attachment", "uuid": "x" + uid, "attachment": {
            "type": "queued_command", "prompt": prompt, "source_uuid": uid, "commandMode": "prompt",
            "origin": {"kind": "human" if human else "agent"}, "humanTurn": human}}) + "\n")


class TypedWhileBusy(unittest.TestCase):
    """Ciprian's checks from his phone were lost while the session worked (25 October 2026)."""

    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}])
        self.p.write(".regista/specs/signup.json", json.dumps(TRY_SPEC))
        self.t = os.path.join(tempfile.mkdtemp(prefix="regista-tr-"), "s1.jsonl")
        open(self.t, "w").close()

    def tearDown(self):
        self.p.cleanup()
        shutil.rmtree(os.path.dirname(self.t), ignore_errors=True)

    def state(self):
        return regista.load_json(regista.requirements_path(self.p.root), {})

    def tool(self, role=None):
        return self.p.hook("pre-tool", {**as_agent(role, "Bash", command="ls"), "transcript_path": self.t})

    def test_queued_checks_are_recorded_once(self):
        queued(self.t, "/regista:verify UI-1 ok fits my phone")
        queued(self.t, "/regista:verify UI-3 ok", human=False, uid="u2")  # not a person: ignored
        ctx = self.tool()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("sent while you were working", ctx)
        self.assertTrue(self.state()["UI-1"]["passes"])
        self.assertEqual(self.state()["UI-1"]["by"], "user (sent while the session was busy)")
        self.assertNotIn("UI-3", self.state())
        self.assertIsNone(self.tool())  # once only
        queued(self.t, "/regista:verify UI-3 ok", uid="u9")
        out = self.p.hook("pre-tool", {**as_agent(None, "Write", file_path=os.path.join(
            self.p.root, ".regista", "gates", "requirements.json")), "transcript_path": self.t})
        self.assertIn("UI-3 as verified", out["hookSpecificOutput"]["permissionDecisionReason"])  # even when refused

    def test_stop_reports_a_queued_check(self):
        queued(self.t, "/regista:verify UI-3 fail the logo is cut off", uid="u3")
        out = self.p.hook("stop", {"transcript_path": self.t})
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("NOT working", out["reason"])
        self.assertFalse(self.state()["UI-3"]["passes"])

    def test_several_checks_in_one_message(self):
        text = "/regista:verify UI-1 ok /regista:verify UI-3 ok\nUI-1 ok looks right"
        self.assertEqual(regista.typed_items(text), [("verify", "UI-1 ok"), ("verify", "UI-3 ok"),
                                                ("verify", "UI-1 ok looks right")])
        self.assertEqual(regista.typed_items("/regista:verify UI-3 fail the button\nis purple"),
                         [("verify", "UI-3 fail the button is purple")])
        out = self.p.hook("user-prompt-expansion", {"command_name": "regista:verify", "command_args": "UI-1 ok",
                                                    "prompt": "/regista:verify UI-1 ok\n/regista:verify UI-3 ok"})
        self.assertIn("UI-3", out["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(self.state()["UI-1"]["passes"] and self.state()["UI-3"]["passes"])

    def test_all_ok_records_every_waiting_check(self):
        queued(self.t, "/regista:verify UI-3 fail colours off", uid="a")
        queued(self.t, "/regista:verify all ok", uid="b")
        ctx = self.tool()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("UI-1 as verified by the user (they typed `all ok`)", ctx)
        self.assertTrue(self.state()["UI-1"]["passes"])
        self.assertFalse(self.state()["UI-3"]["passes"])  # a reported problem isn't swept up by "all ok"
        out = self.p.hook("user-prompt-expansion", {"command_name": "regista:verify", "command_args": "all fail",
                                                    "prompt": "/regista:verify all fail"})
        self.assertIn("doesn't say which check failed", out["hookSpecificOutput"]["additionalContext"])

    def test_manual_requirements_read_as_waiting_not_uncovered(self):
        out = self.p.cli("requirements").stdout
        self.assertIn("waits for the user", out)
        self.assertNotIn("UI-1           manual not covered", out)


class Changes(unittest.TestCase):
    """Scope changes during a run: typed by the user, shown until they're in the plan, ordered by After:."""

    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}])
        self.t = os.path.join(tempfile.mkdtemp(prefix="regista-tr-"), "s1.jsonl")
        open(self.t, "w").close()

    def tearDown(self):
        self.p.cleanup()
        shutil.rmtree(os.path.dirname(self.t), ignore_errors=True)

    def test_typed_and_queued_changes_are_recorded_and_shown(self):
        out = self.p.hook("user-prompt-expansion", {"command_name": "regista:change",
                                                    "command_args": "add club crests, build it last",
                                                    "prompt": "/regista:change add club crests, build it last"})
        self.assertIn("recorded change 1", out["hookSpecificOutput"]["additionalContext"])
        queued(self.t, "/regista:change the bottom bar gets five slots")
        self.p.hook("pre-tool", {**as_agent(None, "Bash", command="ls"), "transcript_path": self.t})
        start = self.p.hook("session-start", {})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("club crests", start)
        self.assertIn("five slots", start)
        self.p.cli("run", "start")
        self.assertIn("five slots", self.p.hook("stop", {})["reason"])
        self.assertEqual(self.p.cli("change", "done", "1", "--where", "M9").returncode, 0)
        self.assertNotIn("club crests", self.p.hook("session-start", {})["hookSpecificOutput"]["additionalContext"])

    def test_only_the_user_adds_changes(self):
        for role in (None, "builder"):
            self.assertEqual(decision(self.p.hook("pre-tool", as_agent(role, "Bash",
                                                                       command=f'python "{TOOL}" change add more pages'))),
                             "deny", role)
        self.assertEqual(decision(self.p.hook("pre-tool", as_agent(None, "Skill", skill="regista:change"))), "deny")
        self.assertEqual(decision(self.p.hook("pre-tool", as_agent("builder", "Bash",
                                                                   command=f'python "{TOOL}" change done 1'))), "deny")
        self.assertEqual(self.p.cli("change", "add", "a", "price", "page").returncode, 0)  # the user's own terminal
        self.assertIn("price page", self.p.cli("change").stdout)

    def test_after_rules_set_the_build_order(self):
        plan = ("# Plan\n\n## M1 — Engine\n- [x] core\n\n## M9 — Crests\nAfter: all\n- [ ] tiles\n\n"
                "## M2 — Prices\nAfter: M3\n- [ ] page\n\n## M3 — What-if\n- [ ] toggles\n")
        self.p.commit(".regista/plan.md", plan, "plan")
        out = self.p.cli("run").stdout
        order = [ln.split(" ")[1] for ln in out.splitlines() if ln.startswith("- M")]
        self.assertEqual(order, ["M1", "M3", "M2", "M9"])
        self.assertIn("M9 — Crests (after every other milestone)", out)
        self.assertIn("Next in build order: M3.", out)


class RunOutcome(unittest.TestCase):
    def test_a_run_that_only_waits_on_the_user_says_so(self):
        p = Project(gates=[{"name": "tests", "run": "echo ok"}, E2E_GATE])
        try:
            p.commit(".regista/plan.md", "# Plan\n\n## M1 — Sign-up\n- [x] form\n", "plan")
            p.write(".regista/specs/signup.json", json.dumps(TRY_SPEC))
            p.write("e2e/signup_test.sh", "# covers: UI-2\n")
            self.assertIsNone(decision(p.hook("pre-tool", as_agent(None, "Bash", command="git add -A && git commit -m x"))))
            self.assertIn("waits only on the user's checks", p.cli("run").stdout)
            p.cli("run", "start")
            r = p.cli("run", "end", "--reason", "only phone checks left")
            self.assertIn("ended (waiting on you)", r.stdout)
        finally:
            p.cleanup()


class Deploy(unittest.TestCase):
    """Two FPL bugs were live while the repository was right: deploys run the live checks."""

    def setUp(self):
        self.live = tempfile.mktemp(prefix="regista-live-")
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"},
                                {"name": "live", "run": f"grep -q fresh {self.live}", "when": ["deploy"]}],
                         deploy={"run": f"cat version.txt > {self.live}"})
        self.p.commit("version.txt", "fresh\n", "v1")

    def tearDown(self):
        self.p.cleanup()
        if os.path.exists(self.live):
            os.remove(self.live)

    def test_deploy_then_live_checks(self):
        r = self.p.cli("deploy")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("live: PASS", r.stdout)
        self.assertIn("live checks passed; it's the latest code", self.p.cli("run").stdout)
        self.p.commit("src/a.py", "x = 1\n", "feat")
        self.assertIn("1 code commit(s) since aren't deployed", self.p.cli("status").stdout)
        self.p.commit("version.txt", "stale\n", "break the live result")
        r = self.p.cli("deploy")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("live checks FAILED", self.p.cli("run").stdout)

    def test_deploy_ships_commits_only_and_only_through_the_harness(self):
        self.p.write("src/a.py", "x = 1\n")
        r = self.p.cli("deploy")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Commit first", r.stdout)
        out = self.p.hook("pre-tool", as_agent(None, "Bash", command=f"cat version.txt > {self.live}"))
        self.assertEqual(decision(out), "deny")
        self.assertIn("regista.py", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(decision(self.p.hook("pre-tool", as_agent("builder", "Bash", command=f'python "{TOOL}" deploy'))),
                         "deny")
        self.assertIsNone(decision(self.p.hook("pre-tool", as_agent(None, "Bash", command=f'python "{TOOL}" deploy'))))

    def test_plain_gate_runs_skip_live_checks(self):
        self.assertNotIn("live", self.p.cli("gate").stdout)


LAUNCHER = os.path.join(os.path.dirname(TOOL), "regista")


FAKE = r'''#!PYTHON
"""Stand-in for `claude` and `tmux` in the fleet tests: records each call and keeps a session list."""
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

FLEET_PLAN = """# Plan

## M1 — Base
- [x] base step

## M2 — Pages
- [ ] the pages

## M3 — Charts
- [ ] the charts

## M4 — Polish
After: M2
- [ ] polish
"""


def jload(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def jsave(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


class Fleet(unittest.TestCase):
    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "test ! -f broken.txt", "when": ["commit"]}],
                         review={"required_for": []}, requirements={"enforce_coverage": False,
                                                                     "milestone_needs_e2e": False})
        self.p.commit(".regista/plan.md", FLEET_PLAN, "plan")
        self.fake = tempfile.mkdtemp(prefix="regista-fake-")
        for tool in ("claude", "tmux"):
            path = os.path.join(self.fake, tool)
            with open(path, "w", encoding="utf-8") as f:
                f.write(FAKE.replace("PYTHON", sys.executable))
            os.chmod(path, 0o755)
        self.env = {"REGISTA_CLAUDE": os.path.join(self.fake, "claude"), "REGISTA_TMUX": os.path.join(self.fake, "tmux"),
                    "FAKE_DIR": self.fake, "REGISTA_LAUNCH_WAIT": "3", "CLAUDE_CODE_SESSION_ID": "overseer-1"}

    def tearDown(self):
        sh(self.p.root, "git", "worktree", "prune")
        self.p.cleanup()
        shutil.rmtree(self.fake, ignore_errors=True)

    def fleet(self, *args, cwd=None):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        env.update(self.env)
        return subprocess.run([PY, TOOL, "fleet", *args], cwd=cwd or self.p.root, capture_output=True, text=True,
                              encoding="utf-8", env=env)

    def hook(self, event, payload, cwd=None):
        return self.p.hook(event, {"cwd": cwd or self.p.root, **payload}, extra_env=self.env)

    def calls(self, tool):
        with open(os.path.join(self.fake, "calls.jsonl"), encoding="utf-8") as f:
            return [c for c in map(json.loads, f) if c["tool"] == tool]

    def wt(self, name="m2"):
        return os.path.join(self.p.root, ".regista", "worktrees", name)

    def spawn(self, target="M2", *extra):
        # the overseer's hook sees it run `regista.py fleet` and notes its session and permission mode
        self.hook("pre-tool", {**bash(f'python3 "{TOOL}" fleet spawn {target}'), "session_id": "overseer-1",
                               "permission_mode": "acceptEdits"})
        return self.fleet("spawn", target, *extra)

    def test_spawn_starts_a_worker_in_its_own_worktree(self):  # covers: CODE-2, CODE-3
        r = self.spawn()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("https://claude.ai/code/session_TEST123", r.stdout)
        self.assertTrue(os.path.isfile(os.path.join(self.wt(), "README.md")))
        self.assertEqual(sh(self.wt(), "git", "rev-parse", "--abbrev-ref", "HEAD").strip(), "regista/m2")
        info = jload(os.path.join(self.wt(), ".regista", "worker.json"))
        self.assertEqual((info["milestone"], info["slot"], info["overseer_root"]), ("M2", 1, self.p.root))
        self.assertTrue(jload(os.path.join(self.wt(), ".regista", "run.json"))["active"])
        new = self.calls("tmux")[0]["args"]
        self.assertEqual(new[:2], ["new-session", "-d"])
        self.assertIn("--remote-control", new[-1])
        self.assertIn("--permission-mode acceptEdits", new[-1])  # the overseer's mode, so messages flow
        self.assertIn("/regista:build M2", new[-1])
        self.assertEqual(sh(self.p.root, "git", "status", "--porcelain").strip(), "")  # the overseer's tree stays clean
        with open(os.path.join(self.p.root, ".git", "info", "exclude"), encoding="utf-8") as f:
            exclude = f.read()
        self.assertIn("# Regista workers", exclude)  # the ignore entry for the worktrees, under the new name
        self.assertIn(".regista/", exclude)
        self.assertNotIn("aedalus", exclude)
        w = jload(os.path.join(self.p.root, ".regista", "fleet", "workers.json"))["m2"]
        self.assertEqual((w["state"], w["session_name"]), ("running", "demo-m2"))
        self.assertTrue(w["session_id"])
        status = self.fleet().stdout
        self.assertIn("m2: M2 — Pages", status)
        self.assertIn("M3 (Charts)", status)  # ready: nothing holds it back
        self.assertNotIn("M4 (", status)  # After: M2, which isn't merged yet

    def test_spawn_with_the_old_variable_names(self):  # covers: CODE-6
        # stand-ins on PATH that do nothing, so a missed fallback can't start the real tools
        tools = sorted(os.listdir(self.fake))  # the two fake tools
        decoy = os.path.join(self.fake, "decoy")
        os.makedirs(decoy)
        for tool in tools:
            path = os.path.join(decoy, tool)
            with open(path, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\nexit 97\n")
            os.chmod(path, 0o755)
        self.env = {k.replace("REGISTA_", "DAEDALUS_"): v for k, v in self.env.items()}
        self.assertIn("DAEDALUS_TMUX", self.env)
        with clean_environ(PATH=decoy + os.pathsep + os.environ.get("PATH", "")):
            r = self.spawn()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.fake, "calls.jsonl")), "the fake tools were never called")
        self.assertEqual(self.calls("tmux")[0]["args"][:2], ["new-session", "-d"])
        self.assertIn("session_TEST123", r.stdout)

    def test_spawn_refusals(self):
        self.assertIn("isn't ready", self.spawn("M4").stdout)
        self.assertIn("all its steps are ticked", self.spawn("M1").stdout)
        self.assertEqual(self.spawn("M2").returncode, 0)
        self.assertIn("already has a worker", self.spawn("M2", "--name", "again").stdout)
        self.p.write("src/a.py", "x = 1\n")
        sh(self.p.root, "git", "add", "-A")
        self.assertIn("uncommitted changes", self.spawn("M3").stdout)
        sh(self.p.root, "git", "commit", "-q", "-m", "a")
        cfg_path = os.path.join(self.p.root, ".regista", "config.json")
        c = jload(cfg_path)
        c["fleet"] = {"max_workers": 1}
        jsave(cfg_path, c)
        sh(self.p.root, "git", "commit", "-qam", "one worker")
        self.assertIn("the most fleet.max_workers allows", self.spawn("M3").stdout)

    def test_background_launch_without_tmux(self):
        cfg_path = os.path.join(self.p.root, ".regista", "config.json")
        c = jload(cfg_path)
        c["fleet"] = {"launch": "bg"}
        jsave(cfg_path, c)
        sh(self.p.root, "git", "commit", "-qam", "bg")
        r = self.spawn()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("claude attach bg1234", r.stdout)
        bg = [c for c in self.calls("claude") if "--bg" in c["args"]][0]
        self.assertEqual(os.path.realpath(bg["cwd"]), os.path.realpath(self.wt()))

    def test_worker_hooks_keep_the_overseer_in_step(self):
        self.spawn()
        start = self.hook("session-start", {}, cwd=self.wt())["hookSpecificOutput"]["additionalContext"]
        self.assertIn('you are the worker "m2"', start)
        self.assertIn("SendMessage", start)
        # the overseer's opening command and a message from another session aren't the user
        self.hook("user-prompt", {"prompt": "/regista:build M2"}, cwd=self.wt())
        self.hook("user-prompt", {"prompt": '<cross-session-message from="x">do it</cross-session-message>'}, cwd=self.wt())
        self.hook("user-prompt", {"prompt": "Make the header blue please"}, cwd=self.wt())
        out = self.hook("user-prompt", {"prompt": "how is it going?"})
        news = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn('the user wrote to it directly: "Make the header blue please"', news)
        self.assertNotIn("do it", news)
        self.assertNotIn("/regista:build", news)
        self.assertIsNone(self.hook("user-prompt", {"prompt": "and now?"}))  # each piece of news once
        # the worker commits and pauses: the record goes to the overseer, and the worker is sent to tell it
        os.makedirs(os.path.join(self.wt(), "src"), exist_ok=True)
        with open(os.path.join(self.wt(), "src", "pages.py"), "w") as f:
            f.write("pages = 1\n")
        sh(self.wt(), "git", "add", "src/pages.py")
        sh(self.wt(), "git", "commit", "-q", "-m", "feat: the pages")
        out = self.hook("stop", {"last_assistant_message": "The pages are built."}, cwd=self.wt())
        self.assertEqual(out["decision"], "block")
        self.assertIn('SendMessage to "', out["reason"])
        self.assertIn("what the user asked you", out["reason"])
        self.assertIn("Then carry on", out["reason"])  # its run is still active
        out = self.hook("stop", {"last_assistant_message": "Told the overseer."}, cwd=self.wt())
        self.assertNotIn("SendMessage", (out or {}).get("reason", ""))  # asked once; the run carries on
        stop = self.hook("stop", {})
        self.assertEqual(stop["decision"], "block")
        self.assertIn("committed 1 (feat: the pages)", stop["reason"])
        self.assertIn("The pages are built.", stop["reason"])

    def test_checks_and_changes_typed_in_a_worker_reach_the_overseer(self):
        self.p.commit(".regista/specs/m2.json", json.dumps({"milestone": "M2", "requirements": [
            {"id": "UI-1", "text": "looks right", "verify": "manual"}]}), "spec")
        self.spawn()
        self.hook("user-prompt-expansion", {"command_name": "regista:verify", "prompt": "/regista:verify UI-1 ok"},
                  cwd=self.wt())
        for root in (self.p.root, self.wt()):
            state = jload(os.path.join(root, ".regista", "gates", "requirements.json"))
            self.assertTrue(state["UI-1"]["passes"], root)
        out = self.hook("user-prompt-expansion", {"command_name": "regista:change",
                                                  "prompt": "/regista:change add a dark mode"}, cwd=self.wt())
        self.assertIn("for the overseer", out["hookSpecificOutput"]["additionalContext"])
        changes = jload(os.path.join(self.p.root, ".regista", "changes.json"))
        self.assertEqual(changes[0]["text"], "add a dark mode")
        self.assertFalse(os.path.exists(os.path.join(self.wt(), ".regista", "changes.json")))

    def test_who_may_do_what(self):
        self.spawn()
        w = self.wt()
        for cmd in (f'python3 "{TOOL}" fleet spawn M3', "git push origin regista/m2", f'python3 "{TOOL}" deploy'):
            self.assertEqual(decision(self.hook("pre-tool", bash(cmd), cwd=w)), "deny", cmd)
        self.assertIsNone(decision(self.hook("pre-tool", bash(f'python3 "{TOOL}" fleet'), cwd=w)))  # looking is fine
        out = self.hook("pre-tool", {"tool_name": "Write", "tool_input": {
            "file_path": os.path.join(self.p.root, "src", "x.py"), "content": ""}}, cwd=w)
        self.assertEqual(decision(out), "deny")
        self.assertIn("overseer's project folder", out["hookSpecificOutput"]["permissionDecisionReason"])
        out = self.hook("pre-tool", {"tool_name": "Edit", "tool_input": {
            "file_path": os.path.join(w, "src", "x.py")}})
        self.assertEqual(decision(out), "deny")  # the overseer doesn't edit a worker's files
        out = self.hook("pre-tool", {**bash(f"echo x > {self.p.root}/.regista/fleet/events.jsonl")})
        self.assertEqual(decision(out), "deny")
        out = self.hook("pre-tool", {**bash(f'python3 "{TOOL}" fleet merge m2'), "agent_id": "a1",
                                     "agent_type": "regista:builder"})
        self.assertEqual(decision(out), "deny")

    def worker_commit(self, rel, text, msg="feat: work"):
        fp = os.path.join(self.wt(), *rel.split("/"))
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)
        sh(self.wt(), "git", "add", "-A")
        sh(self.wt(), "git", "commit", "-q", "-m", msg)

    def test_merge_behind_the_gates(self):
        self.p.commit(".regista/TODO.md", "# Board\n- [ ] decide the colours\n", "board")
        self.spawn()
        self.worker_commit("src/pages.py", "pages = 1\n")
        self.worker_commit(".regista/TODO.md", "# Board\n- [ ] decide the colours\n- [ ] ask about fonts\n", "board")
        self.p.commit("other.txt", "overseer's own commit\n", "other")
        r = self.fleet("merge", "m2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Merged m2", r.stdout)
        self.assertIn("ask about fonts", r.stdout)  # its board lines are handed to the overseer
        self.assertIn("tests: PASS", r.stdout)
        self.assertEqual(sh(self.p.root, "git", "show", "HEAD:src/pages.py"), "pages = 1\n")
        self.assertNotIn("fonts", sh(self.p.root, "git", "show", "HEAD:.regista/TODO.md"))
        self.assertEqual(len(sh(self.p.root, "git", "show", "-s", "--format=%P", "HEAD").split()), 2)
        self.assertEqual(sh(self.p.root, "git", "status", "--porcelain", "--untracked-files=no").strip(), "")
        self.assertIn("Nothing to merge", self.fleet("merge", "m2").stdout)

    def test_merge_refuses_a_failing_gate_and_a_code_conflict(self):
        self.spawn()
        sessions = jload(os.path.join(self.fake, "sessions.json"))
        sessions[0]["status"] = "busy"
        jsave(os.path.join(self.fake, "sessions.json"), sessions)
        self.worker_commit("src/pages.py", "pages = 1\n")
        self.assertIn("m2 is busy", self.fleet("merge", "m2").stdout)
        sessions[0]["status"] = "idle"
        jsave(os.path.join(self.fake, "sessions.json"), sessions)
        self.worker_commit("broken.txt", "")
        r = self.fleet("merge", "m2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("fails a gate", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.p.root, "broken.txt")))
        self.assertEqual(sh(self.p.root, "git", "status", "--porcelain", "--untracked-files=no").strip(), "")
        sh(self.wt(), "git", "rm", "-q", "broken.txt")
        sh(self.wt(), "git", "commit", "-q", "-m", "fix")
        self.worker_commit("README.md", "worker's readme\n")
        self.p.commit("README.md", "overseer's readme\n", "readme")
        r = self.fleet("merge", "m2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("README.md", r.stdout)
        self.assertIn("git merge main", r.stdout)
        self.assertEqual(sh(self.p.root, "git", "status", "--porcelain", "--untracked-files=no").strip(), "")

    def test_harness_records_merge_by_content(self):
        self.assertEqual(regista.merge_json({"a": {"at": "1"}}, {"a": {"at": "2", "passes": False}, "b": 1},
                                       {"a": {"at": "3", "passes": True}, "c": 2}),
                         {"a": {"at": "3", "passes": True}, "b": 1, "c": 2})
        self.assertEqual(regista.merge_json(["x"], ["x", "y"], ["x", "z"]), ["x", "y", "z"])
        self.assertEqual(regista.merge_json({"k": 1, "gone": 1}, {"k": 1, "gone": 1}, {"k": 2}), {"k": 2})

    def test_stop_resume_remove(self):
        self.spawn()
        self.assertEqual(self.fleet("stop", "m2").returncode, 0)
        self.assertIn("m2: M2 — Pages. stopped", self.fleet().stdout)
        r = self.fleet("resume", "m2")
        self.assertEqual(r.returncode, 0, r.stdout)
        starts = [c["args"][-1] for c in self.calls("tmux") if c["args"][0] == "new-session"]
        self.assertIn("--resume", starts[-1])
        self.worker_commit("src/pages.py", "pages = 1\n")
        self.assertIn("aren't merged", self.fleet("remove", "m2").stdout)
        self.assertEqual(self.fleet("merge", "m2").returncode, 0)
        r = self.fleet("remove", "m2")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(os.path.exists(self.wt()))
        self.assertEqual(sh(self.p.root, "git", "branch", "--list", "regista/m2").strip(), "")
        r = self.spawn("M2")  # its steps are still open in the plan
        self.assertIn('"demo-m2-2"', r.stdout)  # a new session gets a name of its own

    def test_a_worker_that_already_reported_isnt_asked_again(self):
        self.spawn()
        tp = os.path.join(self.fake, "worker.jsonl")
        with open(tp, "w", encoding="utf-8") as f:
            for d in ({"type": "user", "message": {"content": "/regista:build M2"}},
                      {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "SendMessage"}]}},
                      {"type": "user", "message": {"content": [{"type": "tool_result", "content": "sent"}]}},
                      {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done; told the overseer."}]}}):
                f.write(json.dumps(d) + "\n")
        jsave(os.path.join(self.wt(), ".regista", "run.json"), {"active": False})
        self.worker_commit("src/pages.py", "pages = 1\n")
        self.assertIsNone(self.hook("stop", {"transcript_path": tp}, cwd=self.wt()))
        self.worker_commit("src/more.py", "more = 1\n")
        out = self.hook("stop", {"transcript_path": os.path.join(self.fake, "none.jsonl")}, cwd=self.wt())
        self.assertIn("Then stop.", out["reason"])  # outside a run, a pause with new work is reported

    def test_build_output_doesnt_keep_a_merged_worktree(self):
        self.spawn()
        self.worker_commit("src/pages.py", "pages = 1\n")
        self.assertEqual(self.fleet("merge", "m2").returncode, 0)
        os.makedirs(os.path.join(self.wt(), "src", "__pycache__"))
        with open(os.path.join(self.wt(), "src", "__pycache__", "pages.cpython-312.pyc"), "w") as f:
            f.write("x")
        with open(os.path.join(self.wt(), "notes.txt"), "w") as f:
            f.write("half a thought")
        r = self.fleet("remove", "m2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("notes.txt", r.stdout)
        self.assertNotIn("pyc", r.stdout)
        os.remove(os.path.join(self.wt(), "notes.txt"))
        self.assertEqual(self.fleet("remove", "m2").returncode, 0)

    def test_init_proposes_how_a_worktree_gets_its_dependencies(self):
        self.assertEqual(regista.worktree_setup(self.p.root), "")
        self.p.write("package-lock.json", "{}")
        self.assertEqual(regista.worktree_setup(self.p.root), "npm ci --silent --no-audit --no-fund")
        self.assertEqual(regista.detect(self.p.root)["fleet"]["setup"], "npm ci --silent --no-audit --no-fund")

    def test_workers_share_one_gate_lock_and_learn_their_slot(self):  # covers: CODE-2
        self.spawn()
        self.assertEqual(regista.gate_lock_path(self.wt()), regista.gate_lock_path(self.p.root))
        self.assertTrue(regista.gate_lock_path(self.wt()).endswith("regista-gates.lock"))
        self.assertEqual(regista.gate_env(self.wt())["REGISTA_SLOT"], "1")
        self.assertEqual(regista.gate_env(self.p.root)["REGISTA_SLOT"], "0")
        self.assertIn("M2", regista.run_text(self.wt(), regista.load_config(self.wt())))
        self.assertNotIn("M3", regista.run_text(self.wt(), regista.load_config(self.wt())))  # a worker's run is its milestone


    def test_a_workers_gates_see_both_names(self):  # covers: CODE-5
        self.spawn()
        out = os.path.join(self.fake, "gate-env.txt")
        with clean_environ():
            env = regista.gate_env(self.wt())
            res = regista.run_gates(self.wt(), regista.load_config(self.wt()), [env_gate(out)])
        self.assertEqual((env["REGISTA_WORKER"], env["REGISTA_SLOT"]), ("m2", "1"))
        self.assertEqual((env["DAEDALUS_WORKER"], env["DAEDALUS_SLOT"]), ("m2", "1"))
        self.assertNotIn("fail", [r["status"] for r in res], res)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.read(), "W=m2 S=1 OW=m2 OS=1")


class Launcher(unittest.TestCase):
    """The FPL server had only `python3`, and every hook failed to start (25 October 2026)."""

    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "exit 1"}])
        self.bin = tempfile.mkdtemp(prefix="regista-bin-")
        for tool in ("sh", "dirname", "git", "bash", "cat"):
            found = shutil.which(tool)
            if found:
                os.symlink(found, os.path.join(self.bin, tool))

    def tearDown(self):
        self.p.cleanup()
        shutil.rmtree(self.bin, ignore_errors=True)

    def run_hook(self, event, payload):
        return subprocess.run(["sh", LAUNCHER, "hook", event], input=json.dumps({"cwd": self.p.root, **payload}),
                              capture_output=True, text=True, cwd=self.p.root, env={"PATH": self.bin, "HOME": os.environ.get("HOME", "")})

    def test_runs_with_only_python3(self):  # covers: CODE-1
        os.symlink(sys.executable, os.path.join(self.bin, "python3"))
        r = self.run_hook("pre-tool", bash("git commit -m x"))
        self.assertEqual(decision(json.loads(r.stdout)), "deny", r.stderr)

    def test_without_python_guards_block_and_others_dont(self):  # covers: CODE-1
        r = self.run_hook("pre-tool", bash("ls"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("needs Python 3.10", r.stderr)
        self.assertEqual(self.run_hook("stop", {}).returncode, 1)  # exit 2 would trap the session

    def test_every_claude_hook_uses_the_launcher(self):  # covers: DOC-2, CODE-1
        hooks = regista.load_json(os.path.join(regista.PLUGIN_ROOT, "hooks", "hooks.json"), {})["hooks"]
        commands = [h["command"] for entries in hooks.values() for e in entries for h in e["hooks"]]
        self.assertEqual(len(commands), 9)
        for c in commands:
            self.assertTrue(c.startswith('sh "${CLAUDE_PLUGIN_ROOT}/scripts/regista" hook '), c)

    def test_every_codex_hook_runs_the_tool_with_python(self):  # covers: DOC-2
        with open(os.path.join(os.path.dirname(os.path.dirname(TOOL)), "hooks", "codex-hooks.json"),
                  encoding="utf-8") as f:
            data = json.load(f)

        def commands(value):
            if isinstance(value, dict):
                for k, v in value.items():
                    if k == "command" and isinstance(v, str):
                        yield v
                    else:
                        yield from commands(v)
            elif isinstance(value, list):
                for v in value:
                    yield from commands(v)

        found = list(commands(data))
        self.assertTrue(found, "hooks/codex-hooks.json registers no command")
        for c in found:
            self.assertTrue(c.startswith('python "${PLUGIN_ROOT}/scripts/regista.py" hook '), c)
            self.assertTrue(c.endswith("--host codex"), c)

    def test_old_tool_files_are_gone(self):  # covers: CODE-1
        scripts = os.path.dirname(TOOL)
        self.assertTrue(os.path.isfile(LAUNCHER))
        self.assertTrue(os.path.isfile(TOOL))
        for old in ("dd", "dd.py"):
            self.assertFalse(os.path.exists(os.path.join(scripts, old)), old)

    def test_launcher_runs_the_tool(self):  # covers: CODE-1
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        r = subprocess.run(["sh", LAUNCHER, "--help"], capture_output=True, text=True, encoding="utf-8", env=env,
                           cwd=tempfile.gettempdir())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Core of the harness", r.stdout)
        self.assertIn("regista.py", r.stdout)
        self.assertNotIn("dd.py", r.stdout)
        out = subprocess.run(["sh", LAUNCHER, "hook", "pre-tool"], input=json.dumps(
            {"cwd": self.p.root, **bash("git push")}), capture_output=True, text=True, encoding="utf-8", env=env,
            cwd=self.p.root)
        self.assertEqual(decision(json.loads(out.stdout)), "ask", out.stdout + out.stderr)


class Detect(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="regista-detect-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, rel, text):
        fp = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)

    def test_empty_folder(self):
        d = regista.detect(self.root)
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
        d = regista.detect(self.root)
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
        d = regista.detect(self.root)
        runs = {g["name"]: g["run"] for g in d["gates"]}
        self.assertEqual(d["kind"], "command-line tool")
        self.assertEqual(runs, {"tests": "uv run pytest -q", "lint": "uv run ruff check ."})
        self.assertEqual(d["roles"]["source"], ["calc/**"])
        self.assertEqual(d["test_files"], 1)
        self.assertTrue(any("end-to-end" in n for n in d["notes"]))

    def test_init_auto(self):  # covers: CODE-2
        sh(self.root, "git", "init", "-q")
        self.write("go.mod", "module x\n")
        r = subprocess.run([PY, TOOL, "init", "--auto", "--name", "X"], cwd=self.root, capture_output=True, text=True,
                           env={**os.environ, "CLAUDE_PROJECT_DIR": self.root})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".regista", "config.json")))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".daedalus")))
        cfg = regista.load_config(self.root)
        self.assertEqual([g["run"] for g in cfg["gates"]], ["go test ./...", "go vet ./..."])
        self.assertIn(".env", cfg["protected"])
        self.assertEqual(regista.load_json(os.path.join(self.root, ".claude", "settings.json"), {})["env"],
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
        self.assertEqual(decision(self.hook("pre-tool", patch("Update File: README.md", "Move to: .regista/gates/x"))),
                         "deny")
        bad = {"tool_name": "apply_patch", "tool_input": {"command": "garbage"}}
        self.assertEqual(decision(self.hook("pre-tool", bad)), "deny")

    def test_codex_agent_roles(self):  # covers: CODE-4
        builder = {"agent_id": "t1", "agent_type": "regista-builder"}
        self.assertEqual(decision(self.hook("pre-tool", {**patch("Add File: tests/test_x.py"), **builder})), "deny")
        self.assertIsNone(self.hook("pre-tool", {**patch("Add File: src/x.py"), **builder}))
        writer = {"agent_id": "t2", "agent_type": "regista-test-writer"}
        self.assertEqual(decision(self.hook("pre-tool", {**patch("Add File: src/x.py"), **writer})), "deny")
        # the orchestrator doesn't edit source; Claude's role names mean nothing here
        self.assertEqual(decision(self.hook("pre-tool", patch("Add File: src/x.py"))), "deny")
        self.assertEqual(decision(self.hook("pre-tool", {**patch("Add File: src/x.py"), "agent_id": "t3",
                                                         "agent_type": "regista:builder"})), "deny")
        spawn = {"tool_name": "collaborationspawn_agent", "tool_input": {"message": "gAAAA"}}
        self.assertEqual(decision(self.hook("pre-tool", {**spawn, **builder})), "deny")
        self.assertIsNone(self.hook("pre-tool", spawn))

    def test_old_host_variable_picks_codex(self):  # covers: CODE-6
        push = {"session_id": "c1", **bash("git push origin main")}
        with clean_environ():
            out = self.p.hook("pre-tool", push, extra_env={"DAEDALUS_HOST": "codex"})  # no --host
            self.assertEqual(decision(out), "deny")
            self.assertIn("Ask the user now", out["hookSpecificOutput"]["permissionDecisionReason"])
            self.assertEqual(decision(self.p.hook("pre-tool", push)), "ask")  # without it: Claude Code

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
        self.assertIsNone(self.hook("user-prompt", {"prompt": "yes", "agent_id": "t1", "agent_type": "regista-builder"}))
        self.assertEqual(decision(self.hook("pre-tool", bash("git push"))), "deny")

    def test_host_settings_are_guarded(self):
        self.assertEqual(decision(self.hook("pre-tool", patch("Update File: .codex/hooks.json"))), "deny")
        self.assertEqual(decision(self.p.hook("pre-tool", as_agent(None, "Edit", file_path=os.path.join(
            self.p.root, ".claude", "settings.json")))), "ask")
        self.assertEqual(decision(self.p.hook("pre-tool", as_agent("builder", "Write", file_path=os.path.join(
            self.p.root, ".codex", "config.toml")))), "deny")

    def test_typed_verify(self):
        self.p.write(".regista/specs/signup.json", json.dumps(TRY_SPEC))
        note = self.hook("user-prompt", {"prompt": "$regista:verify UI-1 ok fits"})["hookSpecificOutput"]
        self.assertIn("recorded UI-1 as verified", note["additionalContext"])
        state = regista.load_json(regista.requirements_path(self.p.root), {})
        self.assertEqual(state["UI-1"]["by"], "user (typed in Codex)")
        # in Claude Code the same prompt records nothing: there only the typed slash command counts
        self.p.hook("user-prompt", {"prompt": "$regista:verify UI-3 ok"})
        self.assertNotIn("UI-3", regista.load_json(regista.requirements_path(self.p.root), {}))

    def test_context_from_the_rollout(self):  # covers: CODE-3
        t = os.path.join(self.p.root, "rollout.jsonl")
        rollout(t, 145_000)  # 56 % of 258,400
        out = self.hook("pre-tool", {"transcript_path": t, **bash("ls")})
        self.assertIn("compacts itself", out["hookSpecificOutput"]["additionalContext"])
        self.assertIn("$regista:save", out["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("/regista:save", out["hookSpecificOutput"]["additionalContext"])
        rollout(t, 197_000)  # 76 %
        self.assertEqual(decision(self.hook("pre-tool", {"transcript_path": t, **bash("pytest -q")})), "deny")
        ctx = self.hook("session-start", {"source": "startup"})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("regista-builder", ctx)
        self.assertIn("codex", regista.load_json(os.path.join(self.p.root, ".regista", "hosts.json"), {}))

    def test_setup_writes_codex_project_files(self):  # covers: CODE-2
        cfg = regista.load_config(self.p.root)
        orig = regista.codex_window, regista.codex_wanted
        regista.codex_window, regista.codex_wanted = (lambda: ("gpt-test", 200_000)), (lambda root: True)
        try:
            self.p.write(".codex/config.toml", 'model_reasoning_effort = "high"\n\n[features]\nhooks = true\n')
            self.p.write("AGENTS.md", "# Rules\nBe nice.\n")
            regista.setup_codex_project(self.p.root, cfg)
            regista.setup_codex_project(self.p.root, cfg)  # idempotent
        finally:
            regista.codex_window, regista.codex_wanted = orig
        toml = regista.read(os.path.join(self.p.root, ".codex", "config.toml"))
        self.assertEqual(regista.toml_top_value(toml, "model_auto_compact_token_limit"), "123500")  # 65 % of 95 % of 200k
        self.assertEqual(regista.toml_top_value(toml, "model_post_turn_compact_threshold_percent"), "65")
        self.assertLess(toml.index("model_auto_compact_token_limit"), toml.index("[features]"))
        self.assertIn('model_reasoning_effort = "high"', toml)
        agent = regista.read(os.path.join(self.p.root, ".codex", "agents", "regista-builder.toml"))
        self.assertIn('name = "regista-builder"', agent)
        self.assertIn("Regista builder", agent)
        md = regista.read(os.path.join(self.p.root, "AGENTS.md"))
        self.assertTrue(md.startswith("# Rules"))
        self.assertEqual(md.count("## Regista harness"), 1)
        self.assertEqual(md.count("<!-- regista:start -->"), 1)
        self.assertEqual(md.count("<!-- regista:end -->"), 1)
        self.assertNotIn("daedalus:start", md)
        for role in ("builder", "test-writer", "reviewer", "researcher"):
            path = os.path.join(self.p.root, ".codex", "agents", f"regista-{role}.toml")
            self.assertIn(f'name = "regista-{role}"', regista.read(path), role)
            self.assertFalse(os.path.exists(os.path.join(self.p.root, ".codex", "agents", f"daedalus-{role}.toml")), role)
        if sys.version_info >= (3, 11):
            import tomllib
            tomllib.loads(toml)
            tomllib.loads(agent)

    def codex_doctor(self, plugin, agent_prefix):
        """Run `doctor` with a stand-in `codex` on the PATH and a home folder whose Codex config enables `plugin`;
        the project has .codex/agents/<agent_prefix><role>.toml for each role."""
        fake = tempfile.mkdtemp(prefix="regista-codex-")
        self.addCleanup(shutil.rmtree, fake, True)
        os.makedirs(os.path.join(fake, "bin"))
        os.makedirs(os.path.join(fake, "home", ".codex"))
        with open(os.path.join(fake, "bin", "codex"), "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho codex-cli 0.50.0\n")
        os.chmod(os.path.join(fake, "bin", "codex"), 0o755)
        with open(os.path.join(fake, "home", ".codex", "config.toml"), "w", encoding="utf-8") as f:
            f.write(f'[features]\nhooks = true\n\n[plugins."{plugin}"]\nenabled = true\n')
        for role in ("builder", "test-writer", "reviewer", "researcher"):
            self.p.write(f".codex/agents/{agent_prefix}{role}.toml", f'name = "{agent_prefix}{role}"\n')
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        env.update(PATH=os.path.join(fake, "bin") + os.pathsep + env.get("PATH", ""), HOME=os.path.join(fake, "home"),
                   CODEX_HOME=os.path.join(fake, "home", ".codex"))
        r = subprocess.run([PY, TOOL, "doctor"], cwd=self.p.root, capture_output=True, text=True, encoding="utf-8",
                           env=env)
        return r.stdout + r.stderr

    def test_codex_doctor_finds_the_regista_plugin_and_agents(self):  # covers: CODE-8
        out = self.codex_doctor("regista@regista", "regista-")
        self.assertIn("Codex agents: builder, test writer, reviewer, researcher", out)
        self.assertNotIn("Codex agents missing", out)
        enabled = [line for line in out.splitlines() if "plugin enabled in Codex" in line]
        self.assertEqual(len(enabled), 1, out)
        self.assertIn("Regista", enabled[0])
        self.assertNotIn("isn't enabled in Codex", out)

    def test_codex_doctor_doesnt_count_the_old_names(self):  # covers: CODE-8
        out = self.codex_doctor("daedalus@daedalus", "daedalus-")
        self.assertIn("Codex agents missing: builder, test-writer, reviewer, researcher", out)
        self.assertIn("Regista plugin isn't enabled in Codex", out)
        self.assertIn("regista@regista", out)


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
            out = p.hook("pre-tool", {"transcript_path": main, "agent_id": "a1", "agent_type": "regista:builder",
                                      "tool_name": "Read", "tool_input": {"file_path": os.path.join(p.root, "README.md")}})
            self.assertEqual(decision(out), "deny")
            self.assertIn("report back", out["hookSpecificOutput"]["permissionDecisionReason"])
        finally:
            p.cleanup()


if __name__ == "__main__":
    unittest.main()
