"""The harness's name lives in one block of constants at the top of scripts/regista.py (NAME-1), and the
typed /<slug>:verify and /<slug>:change commands are recognised from it (NAME-3).

The behaviour checks work from outside: they copy the harness's folders into a temporary folder, rewrite
only the constants block of the copy's scripts/regista.py to a made-up name ("Zephyr"), set up a fresh
project with the copy's `init --auto`, and drive the copy's hooks with JSON on stdin. Every answer must then
say "Zephyr" and never "Daedalus". The same checks run against the unmodified repository with today's names,
which are the Regista ones (CODE-3, CODE-4): its answers must never name Daedalus either.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_names -v"""
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TOOL = os.path.join(REPO, "scripts", "regista.py")
PY = sys.executable

# The folders the spec says make up the harness; the project folder (.regista/ or .daedalus/) and .git are
# never copied.
FOLDERS = ["scripts", "hooks", "skills", "agents", "templates", ".claude-plugin", ".codex-plugin"]

TODAY = {
    "DISPLAY_NAME": "Regista",
    "SLUG": "regista",
    "PROJECT_DIR": ".regista",
    "ENV_PREFIX": "REGISTA_",
    "BRANCH_PREFIX": "regista/",
    "GATE_LOCK": "regista-gates.lock",
}
# The Zephyr copy keeps PROJECT_DIR (".regista"); BRANCH_PREFIX and GATE_LOCK aren't visible in hook answers.
ZEPHYR = {"DISPLAY_NAME": "Zephyr", "SLUG": "zephyr", "ENV_PREFIX": "ZEPHYR_"}

SIGNUP_SPEC = {"step": "signup", "milestone": "M1", "title": "Sign-up", "requirements": [
    {"id": "UI-1", "text": "WHEN a visitor signs up on a phone THEN the page SHALL fit the screen", "verify": "manual",
     "try": {"title": "Sign-up fits a phone screen", "steps": ["Run `npm run dev`", "Open /signup on your phone"],
             "expect": "No sideways scrolling"}},
    {"id": "UI-2", "text": "WHEN the form is sent the user SHALL land on the dashboard", "verify": "e2e",
     "try": {"steps": ["Sign up"], "expect": "The dashboard"}},
    {"id": "UI-3", "text": "The colours SHALL match the brand", "verify": "manual"}]}


def constant_line(name):
    """The pattern for one line of the constants block: NAME = "value" (an optional comment after it)."""
    return re.compile(r'^%s = "([^"\n]*)"[ \t]*(#.*)?$' % re.escape(name), re.M)


# A home folder of our own: this machine's ~/.claude/settings.json may enable the old plugin, and the warning
# about that would show up in every hook answer.
TEST_HOME = tempfile.mkdtemp(prefix="names-home-")
atexit.register(shutil.rmtree, TEST_HOME, True)


def clean_env():
    """The test's environment without the real project, the real home folder or any harness setting leaking in."""
    env = {k: v for k, v in os.environ.items()
           if k != "CLAUDE_PROJECT_DIR" and not k.startswith(("REGISTA_", "DAEDALUS_", "ZEPHYR_"))}
    env.update(HOME=TEST_HOME, CODEX_HOME=os.path.join(TEST_HOME, ".codex"))
    return env


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True,
                          env=clean_env()).stdout


def make_copy(dest, names):
    """Copy the harness's folders into `dest` and rewrite the constants block of its scripts/regista.py.
    Returns the copy's regista.py. Fails if a constant to rewrite isn't in the block."""
    for folder in FOLDERS:
        src = os.path.join(REPO, folder)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dest, folder), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    path = os.path.join(dest, "scripts", "regista.py")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    for name, value in names.items():
        pattern = constant_line(name)
        found = pattern.findall(text)
        if len(found) != 1:
            raise AssertionError(f"scripts/regista.py has {len(found)} lines like {name} = \"...\"; "
                                 f"the constants block needs exactly one")
        text = pattern.sub(lambda m: f'{name} = "{value}"', text)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def as_agent(role_type, tool, **ti):
    """A pre-tool payload; `role_type` is the full agent type (None for the main session)."""
    d = {"tool_name": tool, "tool_input": ti}
    if role_type:
        d.update(agent_id="a1", agent_type=role_type)
    return d


def patch(*headers):
    body = "\n".join(f"*** {h}\n+x" for h in headers)
    return {"tool_name": "apply_patch", "tool_input": {"command": f"*** Begin Patch\n{body}\n*** End Patch"}}


def decision(out):
    return (out or {}).get("hookSpecificOutput", {}).get("permissionDecision")


def queued(path, prompt, human=True, uid="u1"):
    """Append the transcript entry Claude Code writes when a queued message joins a running turn."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "attachment", "uuid": "x" + uid, "attachment": {
            "type": "queued_command", "prompt": prompt, "source_uuid": uid, "commandMode": "prompt",
            "origin": {"kind": "human" if human else "agent"}, "humanTurn": human}}) + "\n")


def transcript(path, tokens, model="claude-sonnet-4-6"):
    """A minimal Claude Code transcript: one main-thread model call that used `tokens` of input."""
    rows = [{"type": "user", "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "message": {"model": model, "usage": {
                "input_tokens": 10, "cache_read_input_tokens": tokens - 110, "cache_creation_input_tokens": 100,
                "output_tokens": 999}}}]
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def rollout(path, tokens, window=258_400):
    """A minimal Codex rollout: a token_count event for the latest model call."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": tokens * 9},
            "last_token_usage": {"input_tokens": tokens - 100, "output_tokens": 100, "total_tokens": tokens},
            "model_context_window": window}}}) + "\n")


class ConstantsBlock(unittest.TestCase):
    """The block itself, in the unmodified repository."""

    def test_module_exposes_todays_names(self):  # covers: NAME-1
        env = clean_env()
        code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import regista; "
                "print(json.dumps({k: getattr(regista, k, None) for k in sys.argv[2:]}))")
        r = subprocess.run([PY, "-c", code, os.path.dirname(TOOL), *TODAY], capture_output=True, text=True,
                           encoding="utf-8", env=env, cwd=REPO)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), TODAY)

    def test_block_is_one_double_quoted_line_each_before_any_code(self):  # covers: NAME-1
        with open(TOOL, encoding="utf-8") as f:
            text = f.read()
        first_def = re.search(r"^(def|class) ", text, re.M)
        last_import = [m.start() for m in re.finditer(r"^(import|from) ", text, re.M)]
        for name, value in TODAY.items():
            found = list(constant_line(name).finditer(text))
            self.assertEqual(len(found), 1, f"{name} should be assigned exactly once, as a double-quoted string")
            self.assertEqual(found[0].group(1), value, name)
            self.assertLess(found[0].start(), first_def.start(), f"{name} should sit at the top, before any function")
            if last_import:
                self.assertGreater(found[0].start(), max(i for i in last_import if i < first_def.start()),
                                   f"{name} should come after the imports")


class NamedHarness:
    """Shared checks, run once against a renamed copy and once against the unmodified repository.
    Subclasses set NAMES (None means the unmodified repository), DISPLAY, SLUG, ENV and OLD (names that
    must never appear in an answer)."""

    NAMES = None
    DISPLAY = SLUG = ENV = ""
    OLD = ()

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="names-test-")
        self.addCleanup(shutil.rmtree, self.base, True)
        if self.NAMES is None:
            self.tool = TOOL
        else:
            copy = os.path.join(self.base, "harness")
            os.makedirs(copy)
            self.tool = make_copy(copy, self.NAMES)
        self.root = os.path.join(self.base, "project")
        os.makedirs(self.root)
        sh(self.root, "git", "init", "-q", "-b", "main")
        sh(self.root, "git", "config", "user.email", "t@example.com")
        sh(self.root, "git", "config", "user.name", "T")
        self.write("app.py", 'print("hello")\n')
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "first commit")
        r = self.cli("init", "--auto")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cfg_path = os.path.join(self.root, ".regista", "config.json")
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        # quick gates and known role folders, so the answers don't depend on what init detected
        cfg.update(gates=[{"name": "tests", "run": "echo ok", "when": ["commit"]}],
                   roles={"source": ["src/**"], "tests": ["tests/**"]})
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "init")
        self.t = os.path.join(self.base, "transcript.jsonl")
        open(self.t, "w").close()

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    # --- helpers ---

    def write(self, rel, text):
        fp = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)

    def cli(self, *args):
        return subprocess.run([PY, self.tool, *args], cwd=self.root, capture_output=True, text=True,
                              encoding="utf-8", env=clean_env())

    def hook(self, event, payload=None, host=None, extra_env=None):
        data = {"cwd": self.root, **(payload or {})}
        env = clean_env()
        env.update(extra_env or {})
        r = subprocess.run([PY, self.tool, "hook", event, *(["--host", host] if host else [])], input=json.dumps(data),
                           cwd=self.root, capture_output=True, text=True, encoding="utf-8", env=env)
        out = json.loads(r.stdout) if r.stdout.strip() else None
        if out is not None:
            self.assertNoOldName(out)
        return out

    def assertNoOldName(self, out):
        text = json.dumps(out, ensure_ascii=False)
        for path in {self.base, os.path.realpath(self.base)}:  # the temp folder's own name doesn't count
            text = text.replace(path, "<tmp>")
        for path in {REPO, os.path.realpath(REPO)}:  # nor where the harness itself is checked out
            text = text.replace(path, "<repo>")
        for old in self.OLD:
            self.assertNotIn(old, text)
            self.assertNotIn(old.lower() + ":", text)
            self.assertIsNone(re.search(re.escape(old.lower()) + r"-(?!gates\.lock)", text), text[:500])
            if old == "Daedalus":  # both run in a project whose folder is .regista/ (CODE-2)
                self.assertNotIn(".daedalus", text)
                self.assertNotIn("dd.py", text)

    def state(self):
        path = os.path.join(self.root, ".regista", "gates", "requirements.json")
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def reason(self, out):
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def context(self, out):
        return out["hookSpecificOutput"]["additionalContext"]

    # --- NAME-1: every answer names the harness from the constants ---

    def test_session_start_names_the_harness(self):  # covers: NAME-1, CODE-3
        out = self.hook("session-start", {"source": "startup"})
        self.assertIn(f"runs on the {self.DISPLAY} harness", self.context(out))

    def test_protected_file_refusal_starts_with_the_name(self):  # covers: NAME-1, CODE-3
        out = self.hook("pre-tool", as_agent(None, "Write", file_path=os.path.join(self.root, ".env"), content="x"))
        self.assertEqual(decision(out), "deny")
        self.assertTrue(self.reason(out).startswith(self.DISPLAY + ": "), self.reason(out))

    def test_push_question_starts_with_the_name(self):  # covers: NAME-1, CODE-3
        out = self.hook("pre-tool", as_agent(None, "Bash", command="git push"))
        self.assertEqual(decision(out), "ask")
        self.assertTrue(self.reason(out).startswith(self.DISPLAY + ": "), self.reason(out))

    def test_stop_after_a_commit_names_the_save_command(self):  # covers: NAME-1, CODE-3
        self.write("src/a.py", "x = 1\n")
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "feat: a")
        out = self.hook("stop", {"stop_hook_active": False})
        self.assertEqual(out.get("decision"), "block")
        self.assertTrue(out["reason"].startswith(self.DISPLAY + ": "), out["reason"])
        self.assertIn(f"/{self.SLUG}:save", out["reason"])

    def test_context_warning_names_the_save_command(self):  # covers: NAME-1, CODE-3
        transcript(self.t, 112_000)  # 56 % of a 200k window
        out = self.hook("pre-tool", {"transcript_path": self.t, "session_id": "s1",
                                     **as_agent(None, "Bash", command="pytest -q")})
        note = self.context(out)
        self.assertIn("compacts itself", note)
        self.assertIn(f"/{self.SLUG}:save", note)

    def test_codex_answers_use_the_dollar_form_and_codex_agent_names(self):  # covers: NAME-1, CODE-3
        rollout(self.t, 145_000)  # 56 % of 258,400
        out = self.hook("pre-tool", {"session_id": "c1", "transcript_path": self.t,
                                     **as_agent(None, "Bash", command="ls")}, host="codex")
        self.assertIn(f"${self.SLUG}:save", self.context(out))
        self.assertNotIn(f"/{self.SLUG}:save", self.context(out))
        ctx = self.context(self.hook("session-start", {"session_id": "c1", "source": "startup"}, host="codex"))
        self.assertIn(f"{self.SLUG}-builder", ctx)
        refused = self.hook("pre-tool", {"session_id": "c1", **patch("Add File: .env")}, host="codex")
        self.assertEqual(decision(refused), "deny")
        self.assertIn(".env is protected", self.reason(refused))
        asked = self.hook("pre-tool", {"session_id": "c1", **as_agent(None, "Bash", command="git push")}, host="codex")
        self.assertIn("Ask the user now", self.reason(asked))

    def test_agent_types_are_recognised_by_their_prefix(self):  # covers: NAME-1, CODE-4
        src, tests = os.path.join(self.root, "src", "a.py"), os.path.join(self.root, "tests", "test_a.py")
        self.assertIsNone(self.hook("pre-tool", as_agent(f"{self.SLUG}:builder", "Write", file_path=src, content="x")))
        self.assertEqual(decision(self.hook("pre-tool", as_agent(f"{self.SLUG}:builder", "Edit", file_path=tests,
                                                                 old_string="a", new_string="b"))), "deny")
        self.assertIsNone(self.hook("pre-tool", as_agent(f"{self.SLUG}:test-writer", "Write", file_path=tests,
                                                         content="x")))
        builder = {"agent_id": "t1", "agent_type": f"{self.SLUG}-builder"}
        self.assertIsNone(self.hook("pre-tool", {"session_id": "c1", **patch("Add File: src/x.py"), **builder},
                                    host="codex"))
        self.assertEqual(decision(self.hook("pre-tool", {"session_id": "c1", **patch("Add File: tests/test_x.py"),
                                                         **builder}, host="codex")), "deny")

    def test_environment_variables_use_the_prefix(self):  # covers: NAME-1
        self.write(".regista/config.json", "{ not json")
        out = self.hook("pre-tool", as_agent(None, "Bash", command="ls"))
        self.assertEqual(decision(out), "deny")
        self.assertIn("Failing closed", self.reason(out))
        self.assertIsNone(self.hook("pre-tool", as_agent(None, "Bash", command="ls"),
                                    extra_env={f"{self.ENV}FAIL_OPEN": "1"}))

    # --- NAME-3: typed and queued commands are recognised under the slug ---

    def test_typed_verify_is_recorded(self):  # covers: NAME-3
        self.write(".regista/specs/signup.json", json.dumps(SIGNUP_SPEC))
        out = self.hook("user-prompt-expansion", {
            "command_name": f"{self.SLUG}:verify", "command_args": "UI-1 ok",
            "prompt": f"/{self.SLUG}:verify UI-1 ok\n/{self.SLUG}:verify UI-3 ok"})
        self.assertIn("UI-3", self.context(out))
        self.assertTrue(self.state()["UI-1"]["passes"])
        self.assertTrue(self.state()["UI-3"]["passes"])

    def test_typed_verify_in_codex_is_recorded(self):  # covers: NAME-3
        self.write(".regista/specs/signup.json", json.dumps(SIGNUP_SPEC))
        out = self.hook("user-prompt", {"session_id": "c1", "prompt": f"${self.SLUG}:verify UI-1 ok fits"},
                        host="codex")
        self.assertIn("recorded UI-1 as verified", self.context(out))
        self.assertEqual(self.state()["UI-1"]["by"], "user (typed in Codex)")

    def test_queued_verify_is_recorded_by_the_pre_tool_hook(self):  # covers: NAME-3
        self.write(".regista/specs/signup.json", json.dumps(SIGNUP_SPEC))
        queued(self.t, f"/{self.SLUG}:verify UI-1 ok fits my phone")
        queued(self.t, f"/{self.SLUG}:verify UI-3 ok", human=False, uid="u2")  # not a person: ignored
        out = self.hook("pre-tool", {**as_agent(None, "Bash", command="ls"), "transcript_path": self.t})
        self.assertIn("sent while you were working", self.context(out))
        self.assertTrue(self.state()["UI-1"]["passes"])
        self.assertEqual(self.state()["UI-1"]["by"], "user (sent while the session was busy)")
        self.assertNotIn("UI-3", self.state())

    def test_queued_verify_is_recorded_by_the_stop_hook(self):  # covers: NAME-3
        self.write(".regista/specs/signup.json", json.dumps(SIGNUP_SPEC))
        queued(self.t, f"/{self.SLUG}:verify UI-3 fail the logo is cut off", uid="u3")
        out = self.hook("stop", {"transcript_path": self.t})
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("NOT working", out["reason"])
        self.assertFalse(self.state()["UI-3"]["passes"])

    def test_typed_and_queued_changes_are_recorded(self):  # covers: NAME-3
        out = self.hook("user-prompt-expansion", {
            "command_name": f"{self.SLUG}:change", "command_args": "add club crests, build it last",
            "prompt": f"/{self.SLUG}:change add club crests, build it last"})
        self.assertIn("recorded change 1", self.context(out))
        queued(self.t, f"/{self.SLUG}:change the bottom bar gets five slots")
        self.hook("pre-tool", {**as_agent(None, "Bash", command="ls"), "transcript_path": self.t})
        start = self.context(self.hook("session-start", {}))
        self.assertIn("club crests", start)
        self.assertIn("five slots", start)
        self.assertIn("five slots", self.cli("change").stdout)


class RenamedCopy(NamedHarness, unittest.TestCase):
    """A copy of the harness whose constants block says Zephyr: every answer follows the block."""
    NAMES = ZEPHYR
    DISPLAY, SLUG, ENV = "Zephyr", "zephyr", "ZEPHYR_"
    OLD = ("Daedalus",)


class UnmodifiedRepository(NamedHarness, unittest.TestCase):
    """The repository as it is: the /regista: and $regista: forms work, and no answer names Daedalus."""
    NAMES = None
    DISPLAY, SLUG, ENV = "Regista", "regista", "REGISTA_"
    OLD = ("Zephyr", "Daedalus")


if __name__ == "__main__":
    unittest.main()
