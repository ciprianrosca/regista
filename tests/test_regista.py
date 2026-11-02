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
        # without the session's project variables: they would point the harness at the real project
        env = {k: v for k, v in os.environ.items() if k not in regista.HOST_PROJECT_VARS}
        return subprocess.run([PY, TOOL, *args], cwd=self.root, capture_output=True, text=True, encoding="utf-8",
                              env=env)

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


def person_env(**extra):
    """A person's own shell, as clean_env, and also without the variables an agent's shell has (Claude Code's
    project variables and CLAUDECODE, Codex's CODEX_* other than CODEX_HOME): the gates themselves run with
    CLAUDECODE=1, and the harness treats a command run from an agent's shell differently."""
    env = {k: v for k, v in clean_env().items()
           if k not in regista.HOST_PROJECT_VARS and k != "CLAUDECODE"
           and not (k.startswith("CODEX_") and k != "CODEX_HOME")}
    env.update(extra)
    return env


def person_environ(**extra):
    """This process's environment made a person's shell the same way, for code run in-process."""
    return mock.patch.dict(os.environ, person_env(**extra), clear=True)


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


class ParallelBuildRefusals(unittest.TestCase):
    """The refusals of the parallel builds (FPL and Regista's own rename, 26 to 28 October 2026) that
    0.9.0 still made wrongly: paths ending in a variable, text inside scripts, commands in other
    repositories and `claude plugin`. Each case keeps what tripped the check. The refusals that were
    right stay refused, and so do the routes these fixes must not open."""

    def setUp(self):
        self.p = Project(protected=[".env", "*.pem"])
        self.tmp = tempfile.mkdtemp(prefix="regista-scratch-")
        os.makedirs(os.path.join(self.p.root, "tests", "fixtures", "news"))
        os.makedirs(os.path.join(self.p.root, "src", "core"))

    def tearDown(self):
        self.p.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def check(self, role, cmd):
        return decision(self.p.hook("pre-tool", as_agent(role, "Bash", command=cmd)))

    def test_paths_ending_in_a_variable_are_unknown_not_the_project(self):
        t, root = self.tmp, self.p.root
        for role, cmd in [
            ("reviewer", f'S={t}; mkdir -p "$S/alt/src/core"; for f in $(ls {root}/src/core); do '
                         f'cat "{root}/src/core/$f" > "$S/alt/src/core/$f"; done; cd "$S/alt/src/core" && '
                         "perl -pi -e 's/a/b/' live.js"),
            ("test-writer", "cd tests/fixtures/news && for f in *.html; do echo \"$f: $(grep -o '<title[^>]*>' $f "
                            "| head -1)\"; done"),
            ("test-writer", f"S={t}/ref; mkdir -p $S/src/core $S/tests && cd $S && cat > src/core/planner.js <<'EOF'\n"
                            f"export const f = 1;\nEOF\nfor f in a.test.js b.test.js; do cp {root}/tests/$f tests/$f; done"),
            ("test-writer", f"S={t}/chk; W={root}; mkdir -p $S/tests && for f in a.test.js b.test.js; do "
                            f"sed -e \"s#'../../src/#'$S/src/#\" $W/tests/unit/$f > $S/tests/$f; done; cd $S && node --test tests/"),
            ("test-writer", "grep -on \"client\\.[a-zA-Z]*(\" tests/*.test.js | awk -F: '{print $3}' | sort | uniq -c"),
            ("test-writer", f"S={t}; for f in $S/*.log; do echo \"== $f\"; grep -E '^# (pass|fail)' $f; done"),
            ("reviewer", f"S={t}; rm -rf $S/cmp && mkdir -p $S/cmp && for n in proj 'Café'; do for v in old new; do "
                         f"d=$S/cmp/$v/r; rm -rf $d; mkdir -p $d; (cd $d && env -u CLAUDE_PROJECT_DIR python3 {TOOL} "
                         "init --name \"$n\" >/dev/null 2>&1); done; diff -r $S/cmp/old/r $S/cmp/new/r && echo same; done"),
        ]:
            with self.subTest(cmd=cmd[:70]):
                self.assertIsNone(self.check(role, cmd))

    def test_text_inside_scripts_is_data(self):
        t = self.tmp
        for role, cmd in [
            ("builder", f"cat > {t}/shots.mjs <<'EOF'\nimport {{ spawn }} from 'node:child_process';\n"
                        "import { mkdtempSync, writeFileSync } from 'node:fs';\nimport { tmpdir } from 'node:os';\n"
                        "import { join } from 'node:path';\nconst dir = mkdtempSync(join(tmpdir(), 'shots-'));\n"
                        "writeFileSync(join(dir, 'rules.json'), '{}');\n"
                        "for (const p of ['home', 'live']) writeFileSync(`${dir}/${p}.html`, '');\n"
                        f"spawn('node', ['src/poller/main.js'], {{ cwd: '{self.p.root}', env: {{ TZ: 'Europe/Bucharest' }} }});\n"
                        f"EOF\nnode {t}/shots.mjs"),
            ("builder", "python3 - <<'EOF'\np='src/client.js'\ns=open(p).read()\n"
                        "new=\"\"\"    summary: (playerId) => get(`/element-summary/${playerId}/`),\n\"\"\"\n"
                        "open(p,'w').write(s+new)\nEOF"),
            ("researcher", "cd tests/fixtures && ls -la; python3 -I -c \"\nimport json\n"
                           "b=json.load(open('bootstrap-static.json'))\nprint({k:b[k] for k in ['id','web_name']})\n\""),
            ("reviewer", "python3 -c \"\nimport json\nd=json.load(open('.regista/reviews/abc.json'))\n"
                         "json.dump(d,open('.regista/reviews/draft.json','w'),indent=2)\n\""),
            ("test-writer", "python3 - <<'EOF'\np='tests/live/home.test.js'\ns=open(p).read()\n"
                            "new='''test(`every mockup page ${name} answers 404`, async () => {\n"
                            "  await page.goto(`${SITE}/`, { timezoneId: 'Europe/Bucharest' });\n"
                            "});\n'''\nopen(p,'w').write(s+new)\nEOF"),
            ("test-writer", "git mv tests/test_dd.py tests/test_regista.py && python3 - <<'EOF'\n"
                            "p = \"tests/test_regista.py\"\nt = open(p).read()\n"
                            "t = t.replace(\"src/dd\\\"\", \"src/regista\\\"\")\nopen(p, \"w\").write(t)\nEOF"),
            ("test-writer", f"cat > {t}/edits.py <<'PYEOF'\nimport sys\np = sys.argv[1]\nt = open(p).read()\n"
                            "t = t.replace('''x''', '''subprocess.run([\"claude\", \"-p\", \"/regista:verify UI-1 ok\"])\n"
                            "codex exec hi''')\nopen(p, \"w\").write(t)\nPYEOF\n"
                            f"python3 {t}/edits.py tests/test_regista.py && python3 -m py_compile tests/test_regista.py"),
            ("test-writer", "python3 - <<'EOF'\np = \"tests/test_docs.py\"\ns = open(p).read()\n"
                            "s += '''\n    r = subprocess.run([PY, TOOL, \"change\", \"add\", \"x\"])  # regista.py change add\n'''\n"
                            "open(p, \"w\").write(s)\nEOF"),
            ("builder", "python3 - <<'EOF'\np = \"src/README.md\"\ns = open(p, encoding=\"utf-8\").read()\n"
                        "s = s.replace(\"\"\"old\"\"\", \"\"\"The gates live in\n`.regista/`, the launcher in\n"
                        "`src/regista`.\"\"\")\nopen(p, \"w\", encoding=\"utf-8\").write(s)\nEOF"),
            (None, "git rm -q .regista/specs/m3-old.json && python3 - <<'PY'\nimport json\nS=\".regista/specs/\"\n"
                   "text = \"\"\"Spec:\n`.regista/` holds the config that names the gates.\"\"\"\n"
                   "json.dump({\"title\": text}, open(S + \"m3-new.json\", \"w\"))\nPY"),
            ("test-writer", "python3 - <<'EOF'\np = \"tests/e2e.sh\"\ns = open(p).read()\n"
                            "s = s.replace('''# helpers''', '''# A home of our own: `~/.claude/settings.json`, "
                            "`.claude/settings.json` and `.codex/config.toml` may enable the old plugin;\n"
                            "grep -c old \"$ROOT/src/a.py\"''')\nopen(p, \"w\").write(s)\nEOF"),
            ("builder", f"cat > {t}/check.py <<'EOF'\nimport os, sys, tempfile\n"
                        f"sys.path.insert(0, \"{self.p.root}/tests\")\nt = tempfile.mkdtemp()\n"
                        "os.makedirs(os.path.join(t, \"a\", \"x\"))\nopen(os.path.join(t, \"a\", \"x\", \"f\"), \"w\").write(\"\")\n"
                        "os.symlink(os.path.join(t, \"a\", \"target\"), os.path.join(t, \"a\", \"x\", \"link\"))\n"
                        "print(\".regista/worktrees/target\", \".claude/settings.json\", \".regista/fleet/workers.json\")\n"
                        f"EOF\npython3 {t}/check.py"),
            ("builder", "python3 - <<'EOF'\np=\"src/a.py\"\nb=open(p,\"rb\").read()\n"
                        "old=b\"\"\"'\\\"\\\"\\\"Daedalus core' in head\"\"\"\nnew=b\"\"\"f'core' in head\"\"\"\n"
                        "open(p,\"wb\").write(b.replace(old,new))\nEOF\nclaude plugin validate . 2>&1 | tail -2"),
            (None, "python3 - <<'EOF'\np='.regista/status.md'\ns=open(p).read()\n"
                   "s=s.replace(\"_Last updated_\\n\",\"\"\"_Last updated: 2026-10-28_\n\n"
                   "The harness notes moved from `.daedalus/` to `.regista/`, at Ciprian's yes.\n\"\"\",1)\n"
                   "open(p,'w').write(s)\nEOF\ncat > .regista/state/2026-10-28-moved.md <<'EOF'\n# Handoff\n"
                   "- Ran the gates and committed .regista/gates/requirements.json.\nEOF"),
        ]:
            with self.subTest(role=role, cmd=cmd[:70]):
                self.assertIsNone(self.check(role, cmd))

    def test_commands_in_other_repositories_follow_that_repository(self):
        t = self.tmp
        for role, cmd in [
            ("test-writer", f"S={t}; P=$(mktemp -d -p $S proj-XXXX); cd $P && git init -q -b main && git config "
                            "user.email t@e.com && git config user.name T && echo 'print(1)' > app.py && git add -A && "
                            f"git commit -qm first && env -u CLAUDE_PROJECT_DIR python3 {TOOL} init --auto >/dev/null"),
            ("test-writer", f"cat > {t}/proto.py <<'EOF'\nimport json, os, subprocess, sys, tempfile\n"
                            f"D = \"{TOOL}\"\nenv = {{k: v for k, v in os.environ.items() if k != \"CLAUDE_PROJECT_DIR\"}}\n"
                            "P = tempfile.mkdtemp(dir=sys.argv[1])\n"
                            "def sh(*a): subprocess.run(a, cwd=P, check=True)\n"
                            "open(os.path.join(P, \"app.py\"), \"w\").write(\"print(1)\\n\"); sh(\"git\", \"commit\", \"-qm\", \"first\")\n"
                            "for h in (None, \"codex\"):\n"
                            "    subprocess.run([sys.executable, D, \"hook\", \"session-start\", *([\"--host\", h] if h else [])], "
                            "cwd=P, env=env, input=json.dumps({\"cwd\": P}))\n"
                            f"EOF\npython3 {t}/proto.py {t}"),
            ("test-writer", "python3 - <<'EOF'\nimport os, subprocess, sys, tempfile\nP = tempfile.mkdtemp()\n"
                            "env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_PROJECT_DIR'}\n"
                            f"subprocess.run([sys.executable, '{TOOL}', 'run', 'start'], cwd=P, env=env)\nEOF"),
            ("test-writer", f"mkdir -p {t}/p && cd {t}/p && git init -q -b main && git add -A && git commit -qm first && "
                            f"env -u CLAUDE_PROJECT_DIR python3 {TOOL} init --auto; cat .regista/config.json"),
            ("test-writer", f"cd {t} && rm -rf probe && mkdir probe && cd probe && git init -q -b main && echo x > a.txt && "
                            f"git add -A && git commit -qm first && export -n CLAUDE_PROJECT_DIR; T={TOOL}; "
                            "env -u CLAUDE_PROJECT_DIR python3 $T doctor 2>&1 | head -30"),
            ("reviewer", f"SP={t}; cat > $SP/probe.sh <<'EOF'\nset -u\nSP=$1; TOOL=$2\nH=$(mktemp -d -p $SP); "
                         "P=$(mktemp -d -p $SP)\nexport HOME=$H CODEX_HOME=$H/.codex\nunset CLAUDE_PROJECT_DIR\n"
                         "cd $P && git init -q -b main\npython3 $TOOL init --name Demo >/dev/null\n"
                         "mkdir -p ~/.claude && echo '{}' > ~/.claude/settings.json\ngit add -A && git commit -qm init\n"
                         "python3 $TOOL verify-manual UI-1 --note probe; python3 $TOOL run start\nEOF\n"
                         f"bash $SP/probe.sh $SP {TOOL}"),
            ("test-writer", f"S={t}; cat > $S/probe.sh <<'EOF'\nset -e\nd=$(mktemp -d -p \"$1\")\ncd $d && git init -q -b main\n"
                            "HOME=$d/home CLAUDE_PROJECT_DIR=$d python3 \"$2\" init --name Demo >/dev/null\n"
                            "for f in .gitignore .regista/.gitignore; do echo \"== $f\"; cat $f 2>/dev/null || true; done\n"
                            f"git status --porcelain --ignored\nEOF\nbash $S/probe.sh $S {TOOL}"),
            ("builder", "d=$(mktemp -d) && cd $d && git init -q -b main && mkdir -p .regista/s && echo a > .regista/s/x && "
                        "git add -A && git commit -qm i && git mv .regista .daedalus && git status --porcelain; rm -rf $d"),
            ("reviewer", f"S={t}; rm -rf $S/p $S/h; mkdir -p $S/p $S/h; cd $S/p && git init -q -b main && git add -A && "
                         "git commit -qm first; run(){ env -u CLAUDE_PROJECT_DIR HOME=$S/h CODEX_HOME=$S/h/.codex "
                         f"python3 {TOOL} \"$@\"; }}; run gate; echo rc=$?"),
            ("test-writer", f"cd {t} && d=$(mktemp -d) && cd $d && git init -q -b main && HOME=$d/home CLAUDE_PROJECT_DIR=$d "
                            f"python3 {TOOL} init --name Demo >/dev/null && CLAUDE_PROJECT_DIR=$d python3 {TOOL} "
                            "verify-manual UI-1 --note probe; git -C $d commit -qm x"),
        ]:
            with self.subTest(role=role, cmd=cmd[:70]):
                self.assertIsNone(self.check(role, cmd))

    def test_claude_plugin_commands_arent_sessions(self):
        for role, cmd in [(None, "which claude; claude plugin validate . 2>&1 | tail -3"),
                          ("builder", f"cd {self.p.root}; claude plugin validate . 2>&1 | tail -5; "
                                      "env -u CLAUDE_PROJECT_DIR python3 -m unittest discover -s tests 2>&1 | tail -4"),
                          (None, "claude mcp list"), (None, "claude agents"), (None, "claude doctor"),
                          (None, "claude --version"), (None, "claude plugin install regista@regista"),
                          ("builder", "python3 -c \"import subprocess; subprocess.run(['claude', 'plugin', 'validate', '.'])\"")]:
            with self.subTest(cmd=cmd[:70]):
                self.assertIsNone(self.check(role, cmd))

    def test_right_refusals_stay(self):
        t, root = self.tmp, self.p.root
        denied = [
            # From the builds: records of the gates moved or overwritten, the test writer reading or linking source,
            # a manual check recorded by the agent.
            (None, f"python3 -c \"import json; json.dump({{}}, open('{t}/merged.json', 'w'))\" && cp {t}/merged.json "
                   ".regista/gates/requirements.json && git add .regista/gates/requirements.json"),
            (None, "git checkout --theirs -- .regista/gates/requirements.json && git add .regista/gates/requirements.json"),
            (None, f"mv .regista/gates/requirements.json {t}/requirements-stale.json && git status --short"),
            ("test-writer", f"S={t}/tree; R={root}; mkdir -p $S/src && ln -sfn $R/src/db $S/src/db && "
                            "ln -sf $R/src/core/forecast.js $S/src/core/forecast.js"),
            ("test-writer", "grep -n \"^export\" src/core/planner.js src/core/news.js"),
            ("test-writer", "grep -n \"odds_usage\" -i src/db/*.sql src/db/*.js 2>/dev/null | head"),
            ("test-writer", "python3 - <<'EOF'\nsrc=open(\"src/a.py\",encoding=\"utf-8\").read()\n"
                            "i=src.index('\"\"\"'); print(repr(src[i:i+200]))\nEOF"),
            ("test-writer", f"S={t}/mut && rm -rf $S && mkdir -p $S && cd {root} && cp -r src tests $S/ && "
                            "sed -i 's/>/>=/' $S/src/core/prices.js && cd $S && node --test tests/"),
            (None, f"for id in UI-1 UI-2; do python3 {TOOL} verify-manual $id --note 'confirmed'; done"),
            # Routes the fixes must not open.
            ("builder", "for f in tests/*.py; do echo x > $f; done"),
            ("builder", "echo x > $S/tests/$f"),
            (None, "echo {} > .regista/gates/$name.json"),
            (None, "echo {} > $D/.regista/gates/baseline.json"),
            ("builder", f"cat > {t}/w.py <<'EOF'\nimport sys\nopen(sys.argv[1], 'w').write('')\nEOF\n"
                        f"python3 {t}/w.py tests/test_a.py"),
            ("builder", "node -e \"require('fs').writeFileSync(process.argv[1], 'x')\" tests/a.test.js"),
            ("builder", "python3 - <<'EOF'\nfrom pathlib import Path\nfor f in Path('tests').glob('*.py'):\n"
                        "    f.write_text('')\nEOF"),
            ("builder", "python3 - <<'EOF'\nimport os\nd = 'tests'\nfor n in os.listdir(d):\n"
                        "    os.remove(os.path.join(d, n))\nEOF"),
            ("builder", "python3 -c \"import subprocess; subprocess.run(['sed', '-i', 's/a/b/', 'tests/test_a.py'])\""),
            ("builder", "python3 -c \"import os; os.system('rm -rf tests')\""),
            ("builder", "python3 script.py <<'EOF'\nok\nEOF\nbash -c 'cat > tests/a.py' <<'EOF'\nx\nEOF"),
            ("builder", f"cd {t} && git -C {root} commit -qm x"),
            ("builder", f"cd {t} && cd {root} && git commit -qm x"),
            ("reviewer", f"CLAUDE_PROJECT_DIR={t} true; cd {t} && git -C {root} push"),
            (None, f"cd {t} && python3 {TOOL} verify-manual UI-1 --note ok"),
            (None, f"CLAUDE_PROJECT_DIR={t} true; python3 {TOOL} verify-manual UI-1 --note ok"),
            (None, f"env -u CLAUDE_PROJECT_DIR python3 {TOOL} verify-manual UI-1 --note ok"),
            ("builder", f"python3 -c \"import subprocess; subprocess.run(['python3', '{TOOL}', 'run', 'start'])\""),
            (None, "claude -p 'record UI-1'"),
            (None, "env -u CLAUDE_PROJECT_DIR claude -p hi"),
            (None, "python3 -c \"import subprocess; subprocess.run(['claude', '-p', 'verified'])\""),
            (None, "python3 - <<'EOF'\nimport subprocess, shutil\nC = shutil.which('codex')\nsubprocess.run([C, 'exec', 'x'])\nEOF"),
            (None, "node -e \"require('child_process').spawnSync('claude', ['-p', 'x'])\""),
            (None, "echo SECRET=1 >> .env"),
            (None, "python3 -c \"open('.env', 'a').write('X=1')\""),
            (None, "git commit --no-verify -m x"),
        ]
        os.makedirs(os.path.join(root, "tests"), exist_ok=True)
        for role, cmd in denied:
            with self.subTest(role=role, cmd=cmd[:70]):
                self.assertEqual(self.check(role, cmd), "deny")


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

    def test_gates_run_without_the_sessions_project_folder(self):
        # A gate inherited CLAUDE_PROJECT_DIR, so the harness's own tests ran on the real worktree (lesson 8).
        out = tempfile.mktemp(prefix="regista-env-")
        p = Project(gates=[{"name": "env", "run": f'printf "%s" "${{CLAUDE_PROJECT_DIR-unset}}" > "{out}"'}])
        try:
            with clean_environ(CLAUDE_PROJECT_DIR=p.root):
                self.assertNotIn("CLAUDE_PROJECT_DIR", regista.gate_env(p.root))
            r = subprocess.run([PY, TOOL, "gate", "--fresh"], cwd=p.root, capture_output=True, text=True,
                               encoding="utf-8", env=clean_env(CLAUDE_PROJECT_DIR=p.root))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(regista.read(out), "unset")
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
                {"cwd": fresh, **bash("git add -A && git commit -m 'chore: set up Regista'")}),
                env={**os.environ, "CLAUDE_PROJECT_DIR": fresh})
            self.assertEqual(r.stdout.strip(), "", r.stdout)
        finally:
            shutil.rmtree(fresh, ignore_errors=True)
        self.p = Project(gates=[{"name": "tests", "run": "exit 1"}], no_gates_for=["docs/**"])
        self.p.write(".regista/status.md", "done\n")
        self.p.write("docs/spec.md", "spec\n")
        self.p.write(".regista/gates/requirements.json", "{}\n")  # what a gate run or a deploy commits on its own
        self.p.write(".regista/gates/deployed.json", "{}\n")
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


    def test_a_gate_run_on_a_clean_folder_commits_its_record(self):
        # Gate runs left requirements.json uncommitted, so merges and the folder move refused (lesson 3).
        self.p.write(".regista/specs/stats.json", json.dumps({**SPEC, "requirements": [SPEC["requirements"][0]]}))
        self.p.write("tests/test_stats.py", "# covers: STATS-1\n")
        sh(self.p.root, "git", "add", "-A")
        sh(self.p.root, "git", "commit", "-q", "-m", "stats")
        head = sh(self.p.root, "git", "rev-parse", "HEAD").strip()
        self.p.write(".regista/status.md", "notes\n")
        sh(self.p.root, "git", "add", ".regista/status.md")  # staged by someone else: stays out of the record's commit
        r = self.p.cli("gate")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Committed the updated records", r.stdout)
        self.assertEqual(sh(self.p.root, "git", "rev-list", "--count", f"{head}..HEAD").strip(), "1")
        files = sh(self.p.root, "git", "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(files, [".regista/gates/requirements.json"])
        self.assertIn("STATS-1 now passes", sh(self.p.root, "git", "log", "-1", "--format=%s"))
        self.assertEqual(sh(self.p.root, "git", "status", "--porcelain", "--untracked-files=no").strip(),
                         "M  .regista/status.md")
        cfg = regista.load_config(self.p.root)
        self.assertTrue(regista.harness_only(self.p.root, cfg, files))  # a records-only commit: no gates, no review
        self.assertFalse(any("now passes" in c for c in regista.commits_since(self.p.root, cfg, 0)))  # not new work
        # the same result again: nothing to record, nothing to commit
        self.assertNotIn("Committed", self.p.cli("gate", "--fresh").stdout)
        self.assertEqual(sh(self.p.root, "git", "rev-list", "--count", f"{head}..HEAD").strip(), "1")
        # with work in progress the record is left alone, and the output says so
        self.p.write("tests/test_stats.py", "# covers: STATS-1\nmore\n")
        r = self.p.cli("gate", "--fresh")
        self.assertIn("Requirement results not updated", r.stdout)
        self.assertNotIn("Committed", r.stdout)


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

    def test_the_same_result_by_another_gate_keeps_the_record(self):
        # NAV-1 flipped between "checked live" and "checked by the end-to-end tests" with the same pass (lesson 3).
        live = {"name": "live", "run": "echo ok", "when": ["deploy"], "evidence": "e2e", "tests": ["e2e/**"]}
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}, E2E_GATE, live])
        self.p.write(".regista/specs/stats.json", json.dumps({**SPEC, "requirements": [SPEC["requirements"][1]]}))
        self.p.write("e2e/cli_test.sh", "# covers: STATS-2\n")
        cfg = regista.load_config(self.p.root)
        run = lambda name, status: regista.record_requirements(  # noqa: E731
            self.p.root, cfg, [{"name": name, "status": status}])
        self.assertEqual(run("live", "pass")["STATS-2"]["evidence"], ["live"])
        text = regista.read(regista.requirements_path(self.p.root))
        run("e2e", "pass")
        self.assertEqual(regista.read(regista.requirements_path(self.p.root)), text)  # same pass: untouched
        self.assertFalse(run("e2e", "fail")["STATS-2"]["passes"])  # a different result is recorded
        self.assertEqual(self.state()["STATS-2"]["evidence"], ["e2e"])

    def test_only_a_covers_marker_counts(self):
        # NAME-1 showed as covered because an old test checked the text "must look like NAME-1" (lesson 10).
        self.p.write("tests/test_stats.py", 'assert error == "must look like STATS-1"  # recovers: STATS-2\n')
        self.p.write("e2e/cli_test.sh", "# see STATS-2\n")
        out = self.commit()
        self.assertEqual(decision(out), "deny")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("no test is marked `covers: STATS-1`", reason)
        self.assertIn("tests/test_stats.py names it without marking it", reason)
        self.assertIn("e2e/cli_test.sh, tests/test_stats.py name it without marking it", reason)
        r = self.p.cli("requirements")
        self.assertIn("not covered by any test yet", r.stdout)
        self.assertIn("warning: STATS-1: tests/test_stats.py names it", r.stdout)
        self.p.write("tests/test_stats.py", 'test("empty list (covers: STATS-1)", () => {})\n')
        self.p.write("e2e/cli_test.sh", "# Covers:STATS-2\n")
        self.assertIsNone(decision(self.commit()))

    def test_marker_forms(self):
        cfg = regista.load_config(self.p.root)
        spec = {**SPEC, "requirements": [{"id": f"STATS-{n}", "text": "x", "verify": "test"} for n in range(1, 8)]}
        self.p.write("tests/a.py", "# covers: STATS-1, STATS-2\n'covers: STATS-3 STATS-4'\n")
        self.p.write("tests/b.js", "// covers: STATS-5,STATS-6\n/* STATS-7 is checked elsewhere */\n")
        cov = regista.coverage(self.p.root, cfg, [spec])
        self.assertEqual({rid for rid, c in cov.items() if c["tests"]}, {f"STATS-{n}" for n in range(1, 7)})
        self.assertEqual(cov["STATS-7"], {"tests": [], "e2e": [], "mentions": ["tests/b.js"]})
        self.assertEqual(cov["STATS-1"]["mentions"], [])

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


class AgentsAtWork(unittest.TestCase):
    """Build-all runs ended as "stalled" while the session waited on its agents (FPL and Regista, October 2026)."""

    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}])
        self.p.commit(".regista/plan.md", PLAN, "docs: plan")
        self.p.cli("run", "start")

    def tearDown(self):
        self.p.cleanup()

    def stop(self, again=False, session="s1"):
        return self.p.hook("stop", {"session_id": session, **({"stop_hook_active": True} if again else {})})

    def agent(self, event, agent_id="b1", kind="regista:builder", session="s1", **extra):
        return self.p.hook(event, {"session_id": session, "agent_id": agent_id, "agent_type": kind, **extra})

    def run_state(self):
        return regista.load_json(regista.run_path(self.p.root), {})

    def test_a_stop_while_agents_work_is_quiet_and_not_a_stall(self):
        self.agent("subagent-start")
        self.agent("subagent-start", "x1", "Explore")  # every agent counts, not only ours
        for again in (False, True, True):  # it stopped twice and more without a commit: still not stalled
            self.assertIsNone(self.stop(again))
        self.assertTrue(self.run_state()["active"])
        self.agent("subagent-stop")
        self.assertIsNone(self.stop(True))  # the Explore agent still works
        self.agent("subagent-stop", "x1", "Explore")
        self.assertEqual(self.stop()["decision"], "block")  # nothing running: the usual nudge
        out = self.stop(True)
        self.assertIn("stopped", out["systemMessage"])
        self.assertEqual(self.run_state()["outcome"], "stalled")

    def test_only_this_sessions_agents_count(self):
        self.agent("subagent-start", session="other")
        self.assertEqual(self.stop()["decision"], "block")

    def test_an_agent_that_never_reports_finishing_stops_counting(self):
        self.agent("subagent-start")
        path = os.path.join(self.p.root, ".regista", "agents", "b1.json")
        st = jload(path)
        st["started"] -= regista.AGENT_STALE + 60
        jsave(path, st)
        self.assertEqual(self.stop()["decision"], "block")

    def test_without_a_run_nothing_changes(self):
        self.p.cli("run", "end", "--reason", "done")
        self.agent("subagent-start")
        out = self.stop()
        self.assertEqual(out["decision"], "block")  # the save reminder still applies outside a run
        self.assertIn("since the last handoff", out["reason"])

    def test_a_reviewer_still_records_its_verdict_before_it_finishes(self):
        self.agent("subagent-start", "r1", "regista:reviewer")
        out = self.agent("subagent-stop", "r1", "regista:reviewer")
        self.assertEqual(out["decision"], "block")
        self.assertIsNone(self.stop())  # it's still working
        self.assertIsNone(self.agent("subagent-stop", "r1", "regista:reviewer", stop_hook_active=True))
        self.assertTrue(jload(os.path.join(self.p.root, ".regista", "agents", "r1.json"))["finished"])
        self.assertEqual(self.stop()["decision"], "block")


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
        # a person's shell (no CLAUDE_PROJECT_DIR, no agent's variables) whose input isn't a terminal
        r = subprocess.run([PY, TOOL, "try", "--ask"], cwd=self.p.root, input="y\n\n", capture_output=True, text=True,
                           env=person_env())
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


PLAN_WITH_SCOPE = """# Demo: plan

## M1 — Stats
Steps:
- [x] mean
- [ ] median
- [ ] **Mode** of the numbers

## M2 — CLI
Steps:
- [ ] cli

## Out of scope
- graphs
"""


class PlanCommand(unittest.TestCase):
    """The FPL overseer kept the plan by hand with scripts, and a lost tick held a milestone back (October 2026)."""

    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}], review={"required_for": []},
                         roles={"source": ["src/**"], "tests": ["tests/**"]})
        self.p.commit(".regista/plan.md", PLAN_WITH_SCOPE, "docs: plan")
        self.plan = os.path.join(self.p.root, ".regista", "plan.md")

    def tearDown(self):
        self.p.cleanup()

    def text(self):
        with open(self.plan, encoding="utf-8", newline="") as f:
            return f.read()

    def milestones(self):
        return {m["id"]: m for m in regista.plan_milestones(self.p.root, regista.load_config(self.p.root))}

    def test_add_milestone_writes_the_plans_own_format(self):
        r = self.p.cli("plan", "add-milestone", "Charts", "--after", "m2", "--files", "src/charts/**, tests/test_charts.py",
                       "--gate", "a chart renders in under 1 s", "--step", "bar chart", "--step", "line chart")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Added M3 — Charts to .regista/plan.md, built after M2.", r.stdout)
        text = self.text()
        self.assertIn("## M3 — Charts\nAfter: M2\nFiles: src/charts/**, tests/test_charts.py\n\nGate:\n"
                      "- a chart renders in under 1 s\n\nSteps:\n- [ ] bar chart\n- [ ] line chart\n\n## Out of scope", text)
        m = self.milestones()["M3"]
        self.assertEqual((m["after"], m["files"], m["open_steps"]), (["M2"], ["src/charts/**", "tests/test_charts.py"], 2))
        r = self.p.cli("plan", "add-milestone", "Polish", "--after", "all")
        self.assertIn("Added M4 — Polish", r.stdout)
        self.assertIn("Write its gate (what must be true, measurably) and its steps", r.stdout)
        self.assertIn("names no files", r.stdout)
        self.assertEqual(self.milestones()["M4"]["after"], "all")
        r = self.p.cli("plan", "add-milestone", "Ghost", "--after", "M9")
        self.assertEqual(r.returncode, 1)
        self.assertIn("M9 isn't one", r.stdout)

    def test_a_plan_with_windows_line_endings_keeps_them(self):
        self.p.commit(".regista/plan.md", PLAN_WITH_SCOPE.replace("\n", "\r\n"), "crlf")
        self.assertEqual(self.p.cli("plan", "add-milestone", "Charts", "--step", "bar").returncode, 0)
        self.assertEqual(self.p.cli("plan", "tick", "M1", "2").returncode, 0)
        text = self.text()
        self.assertNotIn("\n", text.replace("\r\n", ""))
        self.assertIn("- [x] median\r\n", text)
        self.assertIn("## M3 — Charts\r\n", text)

    def test_tick_by_number_or_by_text(self):
        r = self.p.cli("plan", "tick", "M1", "2")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(r.stdout.strip(), "Ticked M1, step 2: median")
        self.assertIn("already ticked", self.p.cli("plan", "tick", "M1", "median").stdout)
        self.assertIn("Ticked M1, step 3", self.p.cli("plan", "tick", "m1", "mode").stdout)  # a part only one step has
        self.assertIn("- [x] **Mode** of the numbers", self.text())
        r = self.p.cli("plan", "tick", "M2", "graphs")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no step has that text", r.stdout)
        self.assertIn("1. [ ] cli", r.stdout)
        self.assertIn("has 1 step, not 4", self.p.cli("plan", "tick", "M2", "4").stdout)
        self.assertIn("no milestone M7", self.p.cli("plan", "tick", "M7", "1").stdout)
        ok, msg = regista.plan_tick(self.p.root, "M2", "cli")  # what a merge calls
        self.assertTrue(ok, msg)
        self.assertEqual(self.milestones()["M2"]["open_steps"], 0)

    def test_only_the_orchestrator_runs_the_plan_command(self):
        for cmd in (f'python "{TOOL}" plan tick M1 2', f'python "{TOOL}" plan add-milestone "More"'):
            self.assertEqual(decision(self.p.hook("pre-tool", as_agent("builder", "Bash", command=cmd))), "deny", cmd)
            self.assertIsNone(decision(self.p.hook("pre-tool", as_agent(None, "Bash", command=cmd))), cmd)

    def spec(self, **extra):
        self.p.write(".regista/specs/median.json", json.dumps({"step": "median", "milestone": "M1", "title": "Median",
                                                               "requirements": [{"id": "MED-1", "text": "median works",
                                                                                 "verify": "test"}], **extra}))
        self.p.write("tests/test_median.py", "# covers: MED-1\n")

    def commit_hook(self):
        return self.p.hook("pre-tool", bash('git add -A && git commit -m "feat: median"'))

    def test_the_commit_hook_ticks_the_step_its_spec_names(self):
        self.spec(plan_step="median")
        self.p.write("src/median.py", "def median(xs): ...\n")
        out = self.commit_hook()
        self.assertIsNone(decision(out))
        self.assertIn("Ticked M1, step 2: median", out["hookSpecificOutput"]["additionalContext"])
        self.assertIn("- [x] median", self.text())
        self.assertIn(".regista/plan.md", sh(self.p.root, "git", "diff", "--cached", "--name-only"))  # in this commit

    def test_the_step_by_its_number_or_by_the_specs_title(self):
        self.spec(plan_step=3)
        self.p.write("src/mode.py", "def mode(xs): ...\n")
        self.commit_hook()
        self.assertIn("- [x] **Mode** of the numbers", self.text())
        self.assertIn("- [ ] median", self.text())
        sh(self.p.root, "git", "checkout", "-q", "HEAD", "--", ".regista/plan.md")
        self.spec()  # no plan_step: the title "Median" is the step's text
        self.commit_hook()
        self.assertIn("- [x] median", self.text())

    def test_no_tick_when_it_cant_tell(self):
        for extra in ({"plan_step": "med"}, {"title": "Median of a list", "step": "median-calc"},
                      {"plan_step": "median", "state": "planned"}):
            self.spec(**extra)
            self.p.write("src/median.py", "def median(xs): ...\n")
            out = self.commit_hook()
            self.assertNotIn("Ticked", ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext", ""), extra)
            self.assertIn("- [ ] median", self.text(), extra)
        os.remove(os.path.join(self.p.root, "src", "median.py"))
        self.p.commit("tests/test_median.py", "# covers: MED-1\n", "tests and the planned spec")
        self.spec(plan_step="median")
        self.assertEqual(sh(self.p.root, "git", "status", "--porcelain").strip(), "M .regista/specs/median.json")
        self.commit_hook()  # only the spec changes: no work, so no step is done
        self.assertIn("- [ ] median", self.text())


class ChangeFromTranscript(unittest.TestCase):
    """Ciprian's scope changes were plain sentences to the overseer, and `change add` is refused to every agent."""

    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}])
        self.t = os.path.join(tempfile.mkdtemp(prefix="regista-tr-"), "s1.jsonl")
        self.say("Make the header blue. Also, build the price page last!\n- add club crests")
        self.say("Don't add a dark mode.")
        with open(self.t, "a", encoding="utf-8") as f:
            for d in ({"type": "user", "isMeta": True, "origin": {"kind": "peer", "name": "worker"},
                       "message": {"content": "Another Claude session sent a message: add a login page."}},
                      {"type": "user", "isSidechain": True, "origin": {"kind": "human"},
                       "message": {"content": "add a search box"}},
                      {"type": "user", "message": {"content": "add a footer"}},  # no origin: not known to be a person
                      {"type": "assistant", "message": {"content": [{"type": "text", "text": "add a news feed"}]}}):
                f.write(json.dumps(d) + "\n")

    def tearDown(self):
        self.p.cleanup()
        shutil.rmtree(os.path.dirname(self.t), ignore_errors=True)

    def say(self, text):
        with open(self.t, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "origin": {"kind": "human"},
                                "message": {"content": [{"type": "text", "text": text}]}}) + "\n")

    def check(self, quote, role=None, cmd=None):
        return self.p.hook("pre-tool", {**as_agent(role, "Bash", command=cmd or f'python "{TOOL}" change from-transcript '
                                                                               f'"{quote}"'),
                                        "transcript_path": self.t})

    def test_the_users_own_sentences_are_recorded(self):
        for quote in ("Make the header blue.", "also, build the price page last", "Add club crests",
                      "Make the header blue. Also, build the price page last!"):
            self.assertIsNone(decision(self.check(quote)), quote)
        queued(self.t, "Rename the tab to Fixtures.")  # sent while the session was busy
        self.assertIsNone(decision(self.check("Rename the tab to Fixtures")))
        r = self.p.cli("change", "from-transcript", "Make the header blue.")
        self.assertIn("recorded change 1", r.stdout)
        items = jload(os.path.join(self.p.root, ".regista", "changes.json"))
        self.assertEqual(items[0]["text"], "Make the header blue.")
        self.assertIn("transcript", items[0]["by"])

    def test_anything_else_is_refused(self):
        for quote in ("add a dark mode", "Make the header", "add a login page", "add a search box", "add a footer",
                      "add a news feed", "build the price page"):
            out = self.check(quote)
            self.assertEqual(decision(out), "deny", quote)
        self.assertIn("isn't something the user typed", self.check("add a dark mode")["hookSpecificOutput"]
                      ["permissionDecisionReason"])
        self.assertEqual(decision(self.check("Make the header blue.", role="builder")), "deny")
        self.assertEqual(decision(self.check("", cmd=f'python "{TOOL}" change from-transcript --by me "Make the header '
                                                     'blue."')), "deny")
        out = self.p.hook("pre-tool", {**as_agent(None, "Bash", command=f'python "{TOOL}" change from-transcript '
                                                                         '"Make the header blue."')})
        self.assertEqual(decision(out), "deny")  # no transcript, no proof

    def test_codex_reads_the_users_messages(self):
        t = os.path.join(os.path.dirname(self.t), "rollout.jsonl")
        with open(t, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message",
                                                                 "message": "Build the price page last."}}) + "\n")
            f.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Add a dark mode."}]}}) + "\n")
        for quote, want in (("Build the price page last.", None), ("Add a dark mode.", "deny")):
            out = self.p.hook("pre-tool", {**bash(f'python "{TOOL}" change from-transcript "{quote}"'),
                                           "session_id": "c1", "transcript_path": t}, host="codex")
            self.assertEqual(decision(out), want, quote)


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

    def test_a_deploy_leaves_the_folder_clean(self):
        r = self.p.cli("deploy")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sh(self.p.root, "git", "status", "--porcelain").strip(), "")
        self.assertIn("Record the deploy of", sh(self.p.root, "git", "log", "-1", "--format=%s"))
        self.assertEqual(sh(self.p.root, "git", "show", "--name-only", "--format=", "HEAD").split(),
                         [".regista/gates/deployed.json"])

    def test_plain_gate_runs_skip_live_checks(self):
        self.assertNotIn("live", self.p.cli("gate").stdout)


class GateCommands(unittest.TestCase):
    """Agents ran the end-to-end suite directly, outside the gate lock, and collided on one database (lesson 7)."""

    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "cd web && PGHOST=/tmp npm test --silent"},
                                {"name": "e2e", "run": "npm run e2e --silent", "evidence": "e2e", "tests": ["e2e/**"]}])

    def tearDown(self):
        self.p.cleanup()

    def check(self, cmd, host=None):
        out = self.p.hook("pre-tool", as_agent("builder", "Bash", command=cmd), host=host)
        return decision(out), (out or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason", "")

    def test_running_a_gate_command_is_sent_through_the_harness(self):
        for cmd in ("npm run e2e", "npm run e2e --silent > /tmp/e2e.log 2>&1 &", "cd sub && npm run e2e -- --grep x",
                    "PGHOST=/tmp timeout 900 npm run e2e", 'bash -c "npm run e2e"', "npm test", "npm run test",
                    "npm t -- web/a.test.js"):
            with self.subTest(cmd=cmd):
                verdict, reason = self.check(cmd)
                self.assertEqual(verdict, "deny")
                self.assertIn("gate " + ("e2e" if "e2e" in cmd else "tests"), reason)
                self.assertIn("lock", reason)
        self.assertEqual(self.check("npm run e2e", host="codex")[0], "deny")

    def test_mentions_and_other_commands_are_fine(self):
        for cmd in ('echo "npm run e2e"', 'grep -n "npm run e2e --silent" README.md', "npm run e2e:debug",
                    "npm run build", "node --test e2e/one.test.js", "cd web", f'python "{TOOL}" gate e2e',
                    "npm install"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.check(cmd)[0])


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
        self.p.write(".regista/plan.md", FLEET_PLAN + "\n## M5 — Extra\n- [ ] more\n")  # the plan, not yet committed
        r = self.spawn("M2")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("First committed the plan and the harness's records that had changed (.regista/plan.md)", r.stdout)
        self.assertIn("M5 — Extra", sh(self.wt(), "git", "show", "HEAD:.regista/plan.md"))  # the worker sees it
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
        self.assertIn("1 commit(s) (feat: the pages)", stop["reason"])  # progress is folded (lesson 11), commits named
        self.assertIn("Told the overseer.", stop["reason"])

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
        self.assertIn("m2 is busy", self.fleet("merge", "m2", "--when-idle", "0").stdout)
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
        self.assertIn("fleet sync", r.stdout)  # how the worker brings the project's work into its branch
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

    # --- lesson 1: the overseer runs in the project -------------------------------------------

    def set_sessions(self, update):
        """Change the fake session list the way Claude Code would, in one step (the fake reads it meanwhile)."""
        path = os.path.join(self.fake, "sessions.json")
        sessions = jload(path) if os.path.exists(path) else []
        update(sessions)
        jsave(path + ".tmp", sessions)
        os.replace(path + ".tmp", path)

    def overseer_in(self, folder):
        self.set_sessions(lambda s: s.append({"name": "overseer", "sessionId": "overseer-1", "status": "busy",
                                              "cwd": folder, "kind": "interactive"}))

    def test_fleet_commands_refuse_a_session_started_outside_the_project(self):
        # The FPL overseer ran in ~/personal-assistants and drove the project with `cd ~/fpl && regista.py fleet …`
        # (26 to 28 October 2026): its hooks never ran, so it heard nothing from its workers for 23 hours.
        elsewhere = tempfile.mkdtemp(prefix="regista-elsewhere-")
        self.addCleanup(shutil.rmtree, elsewhere, True)
        self.overseer_in(elsewhere)
        r = self.spawn()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(f"started in {elsewhere}, not in the project", r.stdout)
        self.assertIn(f"`cd {self.p.root} && claude`", r.stdout)
        self.assertIn("/regista:oversee", r.stdout)
        self.assertFalse(os.path.exists(self.wt()))
        self.assertIn("not in the project", self.fleet("merge", "m2").stdout)
        self.assertEqual(self.fleet().returncode, 0)  # looking is fine
        # a plain terminal in the project (no session) runs it as before
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_CODE_SESSION_ID")}
        env.update({k: v for k, v in self.env.items() if k != "CLAUDE_CODE_SESSION_ID"})
        r = subprocess.run([PY, TOOL, "fleet", "spawn", "M3"], cwd=self.p.root, capture_output=True, text=True,
                           encoding="utf-8", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # a session started in the project, or in a folder inside it, may
        self.set_sessions(lambda s: [x.update(cwd=os.path.join(self.p.root, ".regista")) for x in s
                                     if x["sessionId"] == "overseer-1"])
        r = self.spawn()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    # --- lesson 2: merges don't fail over small things -----------------------------------------

    def test_merge_waits_for_a_worker_finishing_its_turn(self):
        # A worker reported "ready" and the merge said "busy" while its turn was ending (4 times, 26 to 27 October).
        import threading
        self.spawn()
        self.worker_commit("src/pages.py", "pages = 1\n")
        self.set_sessions(lambda s: s[0].update(status="busy"))
        later = threading.Timer(2.0, lambda: self.set_sessions(lambda s: s[0].update(status="idle")))
        later.start()
        self.addCleanup(later.cancel)
        r = self.fleet("merge", "m2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Merged m2", r.stdout)
        # a worker that stays busy is still refused, after the wait
        self.worker_commit("src/more.py", "more = 1\n")
        self.set_sessions(lambda s: s[0].update(status="busy"))
        t0 = time.time()
        r = self.fleet("merge", "m2", "--when-idle", "1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("m2 is busy, still after waiting 1 second for its turn to end", r.stdout)
        self.assertGreaterEqual(time.time() - t0, 1)
        self.assertIn("--force", r.stdout)

    def test_merge_commits_the_harness_records_and_sets_aside_one_in_the_way(self):
        # FPL: a results file a gate run left changed or untracked stopped merges; the overseer committed by hand
        # 6 times, and once Ciprian moved the file himself after 41 minutes.
        self.spawn()
        self.worker_commit("src/pages.py", "pages = 1\n")
        self.worker_commit(".regista/gates/requirements.json", json.dumps({"UI-1": {"passes": True, "at": "2"}}))
        self.p.write(".regista/status.md", "# Status\nM2 is being built.\n")
        self.p.write(".regista/gates/requirements.json", json.dumps({"UI-1": {"passes": False, "at": "1"}}))
        self.p.write("src/draft.py", "draft = 1\n")  # untracked work isn't in the way
        with open(os.path.join(self.wt(), ".regista", "TODO.md"), "a", encoding="utf-8") as f:
            f.write("- [ ] the worker's own note\n")  # the worker's uncommitted records aren't merged, so they don't stop it
        r = self.fleet("merge", "m2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("First committed the harness's records that had changed here (.regista/status.md)", r.stdout)
        self.assertIn(".regista/gates/requirements.json was in the way", r.stdout)
        self.assertIn("requirements.json.before-merge", r.stdout)
        log = sh(self.p.root, "git", "log", "--first-parent", "--format=%s", "-2").splitlines()
        self.assertEqual(log[1], "Save the harness's records before merging m2")
        self.assertEqual(sh(self.p.root, "git", "show", "HEAD:.regista/status.md"), "# Status\nM2 is being built.\n")
        self.assertTrue(jload(os.path.join(self.p.root, ".regista", "gates", "requirements.json"))["UI-1"]["passes"])
        aside = jload(os.path.join(self.p.root, ".regista", "gates", "requirements.json.before-merge"))
        self.assertFalse(aside["UI-1"]["passes"])  # the overseer's copy is kept, under another name
        # work that isn't a record still stops the merge
        self.worker_commit("src/more.py", "more = 1\n")
        self.p.commit("src/a.py", "a = 1\n", "a")
        self.p.write("src/a.py", "a = 2\n")
        r = self.fleet("merge", "m2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("uncommitted changes (src/a.py)", r.stdout)

    def test_a_failed_merge_puts_the_record_it_set_aside_back(self):
        self.spawn()
        self.worker_commit(".regista/gates/requirements.json", json.dumps({"UI-1": {"passes": True, "at": "2"}}))
        self.worker_commit("broken.txt", "")
        self.p.write(".regista/gates/requirements.json", '{"mine": 1}')
        r = self.fleet("merge", "m2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("fails a gate", r.stdout)
        self.assertEqual(jload(os.path.join(self.p.root, ".regista", "gates", "requirements.json")), {"mine": 1})
        self.assertFalse(os.path.exists(os.path.join(self.p.root, ".regista", "gates", "requirements.json.before-merge")))

    def test_plan_tick_boxes_merge_from_both_sides(self):
        # FPL: two branches ticked steps in plan.md and the merge stopped on the conflict for about 30 minutes.
        plan = FLEET_PLAN.replace("- [ ] the pages\n", "- [ ] the pages\n- [ ] the menu\n- [ ] the footer\n")
        self.p.commit(".regista/plan.md", plan, "plan")
        self.spawn()
        self.worker_commit(".regista/plan.md", plan.replace("- [ ] the pages", "- [x] the pages"), "tick")
        self.worker_commit("src/pages.py", "pages = 1\n")
        ours = plan.replace("- [ ] the menu", "- [x] the menu").replace("- [ ] the footer", "- [ ] the footer, in blue")
        self.p.commit(".regista/plan.md", ours, "the overseer's plan")
        r = self.fleet("merge", "m2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        merged = sh(self.p.root, "git", "show", "HEAD:.regista/plan.md")
        self.assertEqual(merged, ours.replace("- [ ] the pages", "- [x] the pages"))
        # anything else than tick boxes still stops it
        self.worker_commit(".regista/plan.md", merged.replace("- [ ] the charts", "- [ ] the bar charts"), "reword")
        self.p.commit(".regista/plan.md", merged.replace("- [ ] the charts", "- [x] the charts"), "tick")
        r = self.fleet("merge", "m2")
        self.assertEqual(r.returncode, 1)
        self.assertIn(".regista/plan.md (in the plan, more than tick boxes)", r.stdout)
        self.assertEqual(sh(self.p.root, "git", "status", "--porcelain", "--untracked-files=no").strip(), "")

    def test_merge_plan_ticks(self):
        base = "## M1\n- [ ] a\n- [ ] b\n"
        self.assertEqual(regista.merge_plan_ticks("## M1\n- [x] a\n- [ ] b\n", base, "## M1\n- [ ] a\n- [X] b\n"),
                         "## M1\n- [x] a\n- [x] b\n")
        self.assertEqual(regista.merge_plan_ticks("## M1\n- [ ] a\n- [ ] b\n- [ ] c\n", base, "## M1\n- [x] a\n- [x] b\n"),
                         "## M1\n- [x] a\n- [x] b\n- [ ] c\n")  # the main side's new step, the other side's ticks
        self.assertIsNone(regista.merge_plan_ticks("## M1\n- [x] a\n- [ ] b\n", base, "## M1\n- [ ] a\n- [ ] b2\n"))

    def test_merge_installs_changed_dependencies_before_the_gates(self):
        # FPL: a worker added a solver package; the main folder didn't have it, the gates hung, the merge took 37 minutes.
        log = os.path.join(self.fake, "setup.log")
        cfg_path = os.path.join(self.p.root, ".regista", "config.json")
        c = jload(cfg_path)
        c["fleet"] = {"setup": f'pwd >> "{log}"'}
        c["gates"] = [{"name": "tests", "run": f'test -f "{log}"', "when": ["commit"]}]
        jsave(cfg_path, c)
        sh(self.p.root, "git", "commit", "-qam", "setup")
        self.spawn()
        os.remove(log)  # the worktree's own setup ran; now only the merge's counts
        self.worker_commit("package-lock.json", '{"lockfileVersion": 3}')
        r = self.fleet("merge", "m2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("The merge changed package-lock.json, so the dependencies were installed first", r.stdout)
        with open(log, encoding="utf-8") as f:
            self.assertEqual(os.path.realpath(f.read().strip()), os.path.realpath(self.p.root))
        self.worker_commit("src/pages.py", "pages = 1\n")  # no dependency change: no install
        os.remove(log)
        r = self.fleet("merge", "m2")
        self.assertEqual(r.returncode, 1)  # the gate needs the log, so it shows no install ran
        self.assertNotIn("installed first", r.stdout)

    def test_a_merge_reuses_the_workers_passing_run(self):
        # FPL: 6 of 7 merges re-ran the full gates on code the worker had just passed, about 62 minutes in all.
        runs = os.path.join(self.fake, "runs.log")
        cfg_path = os.path.join(self.p.root, ".regista", "config.json")
        c = jload(cfg_path)
        c["gates"] = [{"name": "tests", "run": f'echo run >> "{runs}"; test ! -f broken.txt', "when": ["commit"]}]
        jsave(cfg_path, c)
        sh(self.p.root, "git", "commit", "-qam", "counting gate")
        self.spawn()
        self.worker_commit("src/pages.py", "pages = 1\n")
        r = subprocess.run([PY, TOOL, "gate"], cwd=self.wt(), capture_output=True, text=True, encoding="utf-8",
                           env={k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.p.root, ".git", "regista-passed.json")))
        r = self.fleet("merge", "m2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("reused", r.stdout)
        with open(runs, encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), 1)
        # once the project has moved on, the merged code is new: the gates run
        self.worker_commit("src/more.py", "more = 1\n")
        subprocess.run([PY, TOOL, "gate"], cwd=self.wt(), capture_output=True, env={k: v for k, v in os.environ.items()
                                                                                   if k != "CLAUDE_PROJECT_DIR"})
        self.p.commit("other.txt", "the overseer's own commit\n", "other")
        r = self.fleet("merge", "m2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("reused", r.stdout)
        with open(runs, encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), 3)

    def test_the_fingerprint_is_the_content_and_nothing_else(self):
        root, cfg = self.p.root, regista.load_config(self.p.root)
        fp = regista.state_fingerprint(root, cfg)
        self.p.write("src/a.py", "a = 1\n")
        dirty = regista.state_fingerprint(root, cfg)
        self.assertNotEqual(dirty, fp)  # an untracked file counts
        sh(root, "git", "add", "-A")
        sh(root, "git", "commit", "-q", "-m", "a")
        self.assertEqual(regista.state_fingerprint(root, cfg), dirty)  # committed or not, the same content
        self.p.write(".regista/status.md", "# Status\nbusy\n")
        self.assertEqual(regista.state_fingerprint(root, cfg), dirty)  # the harness's records don't count
        self.p.write("src/a.py", "a = 2\n")
        self.assertNotEqual(regista.state_fingerprint(root, cfg), dirty)
        self.p.write("src/a.py", "a = 1\n")
        self.assertEqual(regista.state_fingerprint(root, cfg), dirty)
        if os.name != "nt":
            os.chmod(os.path.join(root, "src", "a.py"), 0o755)
            self.assertNotEqual(regista.state_fingerprint(root, cfg), dirty)  # an executable bit changes what runs
            os.chmod(os.path.join(root, "src", "a.py"), 0o644)
        os.rename(os.path.join(root, "src", "a.py"), os.path.join(root, "src", "b.py"))
        sh(root, "git", "add", "-A")
        self.assertNotEqual(regista.state_fingerprint(root, cfg), dirty)  # a rename moves the content

    def test_a_worker_brings_in_the_projects_work(self):
        # Regista's own build: a worker couldn't merge the main branch into its own, because the results file
        # conflicted and the hook refused both ways of resolving it (27 October).
        self.spawn()
        self.worker_commit(".regista/gates/requirements.json", json.dumps({"A-1": {"passes": True, "at": "2"}}))
        self.worker_commit("src/pages.py", "pages = 1\n")
        self.p.commit(".regista/gates/requirements.json", json.dumps({"B-1": {"passes": True, "at": "1"}}), "results")
        self.p.commit("src/other.py", "other = 1\n", "other")
        self.p.commit(".regista/status.md", "# Status\nthe overseer's\n", "status")
        self.worker_commit(".regista/status.md", "# Status\nthe worker's copy\n", "status")
        # the worker's session may run it; its agents may not; hand edits of the results stay refused
        self.assertIsNone(decision(self.hook("pre-tool", bash(f'python3 "{TOOL}" fleet sync'), cwd=self.wt())))
        out = self.hook("pre-tool", {**bash(f'python3 "{TOOL}" fleet sync'), "agent_id": "a1",
                                     "agent_type": "regista:builder"}, cwd=self.wt())
        self.assertEqual(decision(out), "deny")
        out = self.hook("pre-tool", bash("git checkout --theirs .regista/gates/requirements.json"), cwd=self.wt())
        self.assertEqual(decision(out), "deny")
        r = self.fleet("sync", cwd=self.wt())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Brought main's work into your branch", r.stdout)
        self.assertEqual(sh(self.wt(), "git", "show", "HEAD:src/other.py"), "other = 1\n")
        results = json.loads(sh(self.wt(), "git", "show", "HEAD:.regista/gates/requirements.json"))
        self.assertEqual(set(results), {"A-1", "B-1"})
        self.assertEqual(sh(self.wt(), "git", "show", "HEAD:.regista/status.md"), "# Status\nthe overseer's\n")
        self.assertEqual(sh(self.wt(), "git", "status", "--porcelain", "--untracked-files=no").strip(), "")
        self.assertIn("Nothing to bring in", self.fleet("sync", cwd=self.wt()).stdout)
        # a real code conflict: the records are resolved, the code is left to the worker, who commits through the hook
        self.worker_commit("README.md", "the worker's readme\n")
        self.worker_commit(".regista/gates/requirements.json", json.dumps({"A-1": {"passes": False, "at": "3"}}))
        self.p.commit("README.md", "the overseer's readme\n", "readme")
        self.p.commit(".regista/gates/requirements.json", json.dumps({"B-1": {"passes": False, "at": "4"}}), "results")
        r = self.fleet("sync", cwd=self.wt())
        self.assertEqual(r.returncode, 1)
        self.assertIn("changed the same lines in README.md.", r.stdout)
        self.assertIn("`git commit --no-edit`: the commit hook runs the gates and the review", r.stdout)
        self.assertIn("git merge --abort", r.stdout)
        self.assertEqual(sh(self.wt(), "git", "diff", "--name-only", "--diff-filter=U").split(), ["README.md"])
        with open(os.path.join(self.wt(), "README.md"), "w", encoding="utf-8") as f:
            f.write("both readmes\n")
        sh(self.wt(), "git", "add", "README.md")
        sh(self.wt(), "git", "commit", "-q", "--no-edit")
        self.assertEqual(len(sh(self.wt(), "git", "show", "-s", "--format=%P", "HEAD").split()), 2)
        results = json.loads(sh(self.wt(), "git", "show", "HEAD:.regista/gates/requirements.json"))
        self.assertEqual((results["A-1"]["at"], results["B-1"]["at"]), ("3", "4"))
        # a merge the worker started by hand: sync resolves its records
        self.worker_commit(".regista/gates/requirements.json", json.dumps({"A-1": {"passes": True, "at": "5"}}))
        self.p.commit(".regista/gates/requirements.json", json.dumps({"B-1": {"passes": True, "at": "6"}}), "results")
        subprocess.run(["git", "merge", "main"], cwd=self.wt(), capture_output=True)
        r = self.fleet("sync", cwd=self.wt())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("records in the merge under way are resolved", r.stdout)
        sh(self.wt(), "git", "commit", "-q", "--no-edit")
        results = json.loads(sh(self.wt(), "git", "show", "HEAD:.regista/gates/requirements.json"))
        self.assertTrue(results["A-1"]["passes"] and results["B-1"]["passes"])
        # and the overseer's merge of the result goes through
        self.assertEqual(self.fleet("merge", "m2").returncode, 0)
        self.assertEqual(sh(self.p.root, "git", "show", "HEAD:README.md"), "both readmes\n")
        self.assertIn("is for a worker", self.fleet("sync").stdout)

    def test_spawn_warns_when_milestones_share_files(self):
        plan = FLEET_PLAN.replace("## M2 — Pages\n", "## M2 — Pages\nFiles: src/pages/**, tests/test_app.py\n") \
            .replace("## M3 — Charts\n", "## M3 — Charts\nFiles: src/charts/**, `tests/test_app.py`\n")
        plan += "\n## M5 — Feeds\nFiles: src/feeds/**\n- [ ] feeds\n\n## M6 — Help\n- [ ] help\n"
        self.p.commit(".regista/plan.md", plan, "plan with files")
        self.assertNotIn("Warning", self.spawn("M2").stdout)
        r = self.spawn("M3")
        self.assertEqual(r.returncode, 0, r.stdout)  # a warning, not a refusal
        self.assertIn("Warning: M3 and the running worker m2 (M2) change the same files: tests/test_app.py.", r.stdout)
        self.assertIn("`regista.py fleet stop m3`", r.stdout)
        self.assertNotIn("Warning", self.spawn("M5").stdout)
        self.fleet("stop", "m3")
        self.fleet("stop", "m5")
        self.assertIn("M6 names no files", self.spawn("M6").stdout)

    def test_a_worker_doesnt_edit_the_plan_and_its_commits_tick_it(self):
        self.p.commit(".regista/specs/pages.json", json.dumps({"milestone": "M2", "step": "pages", "plan_step": "the pages",
                                                               "requirements": []}), "spec")
        self.spawn()
        w = self.wt()
        plan = os.path.join(w, ".regista", "plan.md")
        for payload in ({"tool_name": "Edit", "tool_input": {"file_path": plan}},
                        bash("sed -i 's/\\[ \\] the pages/[x] the pages/' .regista/plan.md"),
                        bash(f'python3 "{TOOL}" plan tick M2 1')):
            out = self.hook("pre-tool", payload, cwd=w)
            self.assertEqual(decision(out), "deny", payload)
            self.assertIn("overseer", out["hookSpecificOutput"]["permissionDecisionReason"])
        out = self.hook("pre-tool", bash("git checkout -- ."), cwd=w) or {}  # a command on the whole folder
        self.assertNotIn("edit the plan", out.get("hookSpecificOutput", {}).get("permissionDecisionReason", ""))
        r = subprocess.run([PY, TOOL, "plan", "tick", "M2", "1"], cwd=w, capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("overseer", r.stdout + r.stderr)
        self.assertIsNone(decision(self.hook("pre-tool", {"tool_name": "Edit", "tool_input": {
            "file_path": os.path.join(self.p.root, ".regista", "plan.md")}})))  # the overseer's own plan
        # the worker commits the step: the hook ticks it on the worker's branch
        with open(os.path.join(w, ".regista", "specs", "pages.json"), "w", encoding="utf-8") as f:
            json.dump({"milestone": "M2", "step": "pages", "plan_step": "the pages", "title": "Pages",
                       "requirements": []}, f)
        os.makedirs(os.path.join(w, "src"), exist_ok=True)
        with open(os.path.join(w, "src", "pages.py"), "w", encoding="utf-8") as f:
            f.write("pages = 1\n")
        out = self.hook("pre-tool", bash('git add -A && git commit -m "feat: the pages"'), cwd=w)
        self.assertIsNone(decision(out), out)
        sh(w, "git", "add", "-A")
        sh(w, "git", "commit", "-q", "-m", "feat: the pages")
        self.assertIn("- [x] the pages", sh(w, "git", "show", "HEAD:.regista/plan.md"))
        self.assertIn("- [ ] the pages", sh(self.p.root, "git", "show", "HEAD:.regista/plan.md"))

    def test_a_worker_waiting_on_its_agents_isnt_sent_to_report(self):
        self.spawn()
        self.worker_commit("src/pages.py", "pages = 1\n")
        self.hook("subagent-start", {"session_id": "w1", "agent_id": "b7", "agent_type": "regista:builder"}, cwd=self.wt())
        self.assertIsNone(self.hook("stop", {"session_id": "w1"}, cwd=self.wt()))
        self.hook("subagent-stop", {"session_id": "w1", "agent_id": "b7", "agent_type": "regista:builder"}, cwd=self.wt())
        out = self.hook("stop", {"session_id": "w1"}, cwd=self.wt())
        self.assertEqual(out["decision"], "block")  # its agents are done: the run's nudge (and report) are back


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

    def test_a_database_per_worker_is_proposed(self):
        # The FPL workers shared one test database and reset it under each other (lesson 7).
        self.write("package.json", json.dumps({"scripts": {"test": "node --test", "e2e": "node --test tests/e2e/"},
                                               "dependencies": {"pg": "8", "express": "4"}}))
        self.write("tests/e2e/pages.test.js", "")
        d = regista.detect(self.root)
        e2e = next(g for g in d["gates"] if g["name"] == "e2e")
        self.assertEqual((e2e["run"], e2e["evidence"], e2e["tests"]), ("npm run e2e", "e2e", ["tests/e2e/**"]))
        note = next(n for n in d["notes"] if "REGISTA_SLOT" in n)
        self.assertIn("PostgreSQL", note)
        self.assertIn("_test_${REGISTA_SLOT:-0}", note)
        self.assertIn("isolated_gates", note)
        self.write("package.json", json.dumps({"scripts": {"test": "node --test", "e2e": "node --test tests/e2e/"},
                                               "dependencies": {"better-sqlite3": "9"}}))
        self.assertFalse(any("REGISTA_SLOT" in n for n in regista.detect(self.root)["notes"]))  # a file per worktree

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
