"""Each manual check's record says how it was passed: the user looked at that one check by name, passed it with
others in one line, forced it, or an agent relayed it from outside the project's hooks (seen in FPL on 26 and 27
October 2026: 8 forced passes and 4 relayed ones were all recorded as `by: "user"`).
Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_manual_how -v"""
import json
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# test_regista points HOME and CODEX_HOME at a temporary folder when it is imported.
from test_regista import E2E_GATE, PY, TOOL, Project, as_agent, decision, person_env, regista  # noqa: E402
from test_asks import said  # noqa: E402

# Five manual checks in M2 (nothing else, so the milestone passes once they do) and one in M3.
LOOK_SPEC = {"step": "look", "milestone": "M2", "title": "Look and feel", "requirements": [
    {"id": f"UX-{n}", "text": f"The page SHALL look right on screen {n}", "verify": "manual"} for n in range(1, 6)]}
LATER_SPEC = {"step": "later", "milestone": "M3", "title": "Later", "requirements": [
    {"id": "LATE-1", "text": "The report SHALL read well on a phone", "verify": "manual"}]}
# For the milestone report: M2 also needs end-to-end evidence and its steps ticked in the plan.
SHOW_SPEC = {"step": "show", "milestone": "M2", "title": "Show", "requirements": [
    {"id": "SHOW-1", "text": "WHEN run from the CLI the tool SHALL show the page", "verify": "e2e"}]}
PLAN = """# Demo: plan

## M2 — Look and feel
Steps:
- [x] look
- [x] show

## M3 — Later
Steps:
- [ ] later
"""

QUESTION = """UX-1 and UX-2 are ready for you to try.

1. UX-1 — The page looks right on screen 1
2. UX-2 — The page looks right on screen 2

Tell the harness what you saw. When a check works, send its line as it is:
  /regista:verify UX-1 ok
  /regista:verify UX-2 ok"""


class ManualHow(unittest.TestCase):
    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}, E2E_GATE])
        self.addCleanup(self.p.cleanup)
        self.p.write(".regista/specs/look.json", json.dumps(LOOK_SPEC))
        self.p.write(".regista/specs/later.json", json.dumps(LATER_SPEC))

    # --- helpers ------------------------------------------------------------------------------------------------

    def state(self):
        return regista.load_json(regista.requirements_path(self.p.root), {})

    def how(self, rid):
        return self.state().get(rid, {}).get("how")

    def typed(self, args):
        """The user types `/regista:verify <args>` in the session."""
        return self.p.hook("user-prompt-expansion", {"command_name": "regista:verify", "command_args": args,
                                                     "prompt": f"/regista:verify {args}"})

    def manual(self, rid, *extra, note="checked on my phone"):
        """verify-manual run as a subprocess: no terminal, which is what an agent's shell gives it."""
        r = self.p.cli("verify-manual", rid, "--note", note, *extra)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def at_terminal(self, *args, extra=None, typed=None):
        """Run the tool with stdin, stdout and stderr on a pseudo-terminal, as when a person types it. The
        environment is a person's shell: no agent's variables (CLAUDECODE, CODEX_* other than CODEX_HOME) and no
        harness ones, since the gates themselves run with CLAUDECODE=1. `extra` is added after that filtering.
        `typed` is what the person types at the terminal's prompts (lines ending in a newline)."""
        env = {k: v for k, v in os.environ.items()
               if k not in regista.HOST_PROJECT_VARS and k != "CLAUDECODE"
               and not (k.startswith("CODEX_") and k != "CODEX_HOME")
               and not k.startswith(("REGISTA_", "DAEDALUS_"))}
        env.update(extra or {})
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen([PY, TOOL, *args], cwd=self.p.root, env=env,
                                    stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            if typed:
                os.write(master, typed.encode("utf-8"))
            out = b""
            deadline = time.monotonic() + 60
            while True:
                if not select.select([master], [], [], max(0.0, deadline - time.monotonic()))[0]:
                    proc.kill()  # still waiting for input after a minute: don't hang the suite
                    break
                try:
                    chunk = os.read(master, 4096)
                except OSError:  # the terminal closes when the tool exits (EIO on Linux)
                    break
                if not chunk:
                    break
                out += chunk
            code = proc.wait(timeout=60)
        finally:
            os.close(master)
        return code, out.decode("utf-8", "replace")

    def old_record(self, rid):
        """A pass recorded before records said how: written by verify-manual, then the field taken out."""
        self.manual(rid)
        path = regista.requirements_path(self.p.root)
        data = regista.load_json(path, {})
        data[rid].pop("how", None)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def line_for(self, out, rid):
        lines = [ln for ln in out.splitlines() if re.search(rf"\b{re.escape(rid)}\b", ln)]
        self.assertTrue(lines, f"no line names {rid}:\n{out}")
        return " ".join(lines)

    # --- PRE-3: the record says how -----------------------------------------------------------------------------

    def test_a_check_typed_by_name_is_looked(self):  # covers: PRE-3
        self.typed("UX-1 ok fits on my Pixel")
        self.assertTrue(self.state()["UX-1"]["passes"])
        self.assertEqual(self.how("UX-1"), "looked")

    def test_verify_manual_at_a_real_terminal_is_looked(self):  # covers: PRE-3
        code, out = self.at_terminal("verify-manual", "UX-1", "--note", "checked on my phone")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.state()["UX-1"]["passes"], out)
        self.assertEqual(self.how("UX-1"), "looked", out)

    def test_verify_manual_at_a_terminal_in_an_agents_shell_is_relayed(self):  # covers: PRE-3
        code, out = self.at_terminal("verify-manual", "UX-1", "--note", "checked on my phone",
                                     extra={"CLAUDECODE": "1"})
        self.assertEqual(code, 0, out)
        self.assertTrue(self.state()["UX-1"]["passes"], out)
        self.assertEqual(self.how("UX-1"), "relayed", out)

    def test_all_ok_is_bulk(self):  # covers: PRE-3
        self.typed("all ok")
        for rid in ("UX-1", "UX-2", "UX-3", "UX-4", "UX-5", "LATE-1"):
            self.assertTrue(self.state()[rid]["passes"], rid)
            self.assertEqual(self.how(rid), "bulk", rid)

    def test_a_milestone_ok_is_bulk(self):  # covers: PRE-3
        self.typed("UX-1 ok")
        self.typed("M2 ok")
        self.assertEqual(self.how("UX-1"), "looked")  # already passed by name: the bulk line doesn't relabel it
        for rid in ("UX-2", "UX-3", "UX-4", "UX-5"):
            self.assertTrue(self.state()[rid]["passes"], rid)
            self.assertEqual(self.how(rid), "bulk", rid)
        self.assertNotIn("LATE-1", self.state())  # another milestone

    def test_a_plain_ok_to_a_question_naming_several_checks_is_bulk(self):  # covers: PRE-3
        t = os.path.join(tempfile.mkdtemp(prefix="regista-tr-"), "s1.jsonl")
        self.addCleanup(shutil.rmtree, os.path.dirname(t), True)
        open(t, "w").close()
        said(t, "assistant", QUESTION)
        self.p.hook("user-prompt", {"prompt": "All ok", "transcript_path": t})
        for rid in ("UX-1", "UX-2"):
            self.assertTrue(self.state()[rid]["passes"], rid)
            self.assertEqual(self.how(rid), "bulk", rid)

    def test_a_plain_ok_keeps_a_check_already_looked_at(self):  # covers: PRE-3
        self.typed("UX-1 ok fits on my Pixel")
        self.assertEqual(self.how("UX-1"), "looked")
        t = os.path.join(tempfile.mkdtemp(prefix="regista-tr-"), "s1.jsonl")
        self.addCleanup(shutil.rmtree, os.path.dirname(t), True)
        open(t, "w").close()
        said(t, "assistant", QUESTION)
        self.p.hook("user-prompt", {"prompt": "ok", "transcript_path": t})
        self.assertTrue(self.state()["UX-1"]["passes"])
        self.assertEqual(self.how("UX-1"), "looked")  # the plain ok doesn't relabel it
        self.assertTrue(self.state()["UX-2"]["passes"])
        self.assertEqual(self.how("UX-2"), "bulk")

    def test_the_forced_flag_is_forced(self):  # covers: PRE-3
        self.manual("UX-1", "--forced", note="the user said to force it")
        self.assertTrue(self.state()["UX-1"]["passes"])
        self.assertEqual(self.how("UX-1"), "forced")
        self.assertEqual(self.state()["UX-1"]["by"], "user")  # by keeps its meaning

    def test_evidence_saying_it_was_forced_is_forced(self):  # covers: PRE-3
        self.manual("UX-1", note="forced on request, not individually confirmed on his phone")
        self.assertEqual(self.how("UX-1"), "forced")
        self.manual("UX-2", note="Just force all verifies are ok")
        self.assertEqual(self.how("UX-2"), "forced")
        self.manual("UX-3", note="passed with the others, not confirmed individually")
        self.assertEqual(self.how("UX-3"), "forced")
        self.manual("UX-4", note="passed in one go, not checked one by one")
        self.assertEqual(self.how("UX-4"), "forced")

    def test_verify_manual_without_a_terminal_is_relayed(self):  # covers: PRE-3
        self.manual("UX-1", note="the user said all ok over Remote Control")
        self.assertTrue(self.state()["UX-1"]["passes"])  # still passes: this step only records and reports
        self.assertEqual(self.how("UX-1"), "relayed")
        self.assertEqual(self.state()["UX-1"]["by"], "user")

    def test_try_ask_at_a_terminal_in_an_agents_shell_records_nothing(self):  # covers: PRE-3
        code, out = self.at_terminal("try", "--ask", "UX-1", extra={"CLAUDECODE": "1"}, typed="y\n\n\n")
        self.assertNotEqual(code, 0, out)
        self.assertFalse(self.state().get("UX-1", {}).get("passes"), out)
        self.assertNotIn("UX-1", self.state(), out)
        self.assertRegex(out, r"(?i)\byour(self| own)\b[^\n]*terminal",
                         f"should tell the user to run it in their own terminal:\n{out}")

    def test_try_ask_at_a_real_terminal_is_looked(self):  # covers: PRE-3
        code, out = self.at_terminal("try", "--ask", "UX-1", typed="y\n\n\n")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.state().get("UX-1", {}).get("passes"), out)
        self.assertEqual(self.how("UX-1"), "looked", out)

    def test_requirements_shows_how_next_to_each_pass(self):  # covers: PRE-3
        self.typed("UX-1 ok")
        self.manual("UX-3", "--forced")
        self.manual("UX-4")
        self.old_record("UX-5")
        self.typed("M2 ok")  # only UX-2 is still waiting
        r = self.p.cli("requirements")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for rid, how in (("UX-1", "looked"), ("UX-2", "bulk"), ("UX-3", "forced"), ("UX-4", "relayed"),
                         ("UX-5", "unknown")):
            self.assertIn(how, self.line_for(r.stdout, rid), f"{rid} should show {how}:\n{r.stdout}")
        self.assertNotIn("unknown", self.line_for(r.stdout, "UX-1"))

    # --- PRE-7: the milestone counts passes nobody looked at -----------------------------------------------------

    def ready(self):
        """M2's end-to-end requirement passes through a commit the gates allowed, and its steps are ticked."""
        self.p.commit(".regista/plan.md", PLAN, "docs: plan")
        self.p.write(".regista/specs/show.json", json.dumps(SHOW_SPEC))
        self.p.write("e2e/show_test.sh", "# covers: SHOW-1\n")
        out = self.p.hook("pre-tool", as_agent(None, "Bash", command="git add -A && git commit -m feat"))
        self.assertIsNone(decision(out), out)
        self.assertTrue(self.state().get("SHOW-1", {}).get("passes"), self.state())

    def not_looked_line(self, out, n):
        """A line of the milestone report that gives n as a count of manual checks."""
        pat = re.compile(rf"\b{n}\b[^\n]*\b(manual|checks?)\b|\b(manual|checks?)\b[^\n]*\b{n}\b", re.IGNORECASE)
        return [ln for ln in out.splitlines() if pat.search(ln) and re.search(r"look", ln, re.IGNORECASE)]

    def test_milestone_counts_the_passes_that_were_not_looked(self):  # covers: PRE-7
        self.ready()
        self.typed("UX-1 ok")        # looked
        self.manual("UX-3", "--forced")  # forced
        self.manual("UX-4")          # relayed
        self.old_record("UX-5")      # unknown
        self.typed("M2 ok")          # UX-2: bulk
        r = self.p.cli("milestone", "M2")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)  # which requirements pass doesn't change
        self.assertTrue(self.not_looked_line(out, 4), f"the report should say 4 checks weren't looked at:\n{out}")

    def test_milestone_count_follows_the_records(self):  # covers: PRE-7
        self.ready()
        for rid in ("UX-1", "UX-2", "UX-3", "UX-4"):
            self.typed(f"{rid} ok")  # looked
        self.manual("UX-5")          # relayed
        r = self.p.cli("milestone", "M2")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertTrue(self.not_looked_line(out, 1), f"the report should say 1 check wasn't looked at:\n{out}")
        self.assertFalse(self.not_looked_line(out, 4), out)


# PRE-8: before this, an agent could pipe a made-up `/regista:verify UX-1 ok` into the user-prompt hooks itself and
# have the check recorded as looked at. Only Claude Code and Codex call the hooks; an agent's shell never does.
BY_HAND = [
    "python3 scripts/regista.py hook user-prompt-expansion",
    "python scripts/regista.py hook user-prompt --host codex",
    "sh scripts/regista hook stop",
    'python3 "/home/someone/.claude/plugins/cache/regista/scripts/regista.py" hook user-prompt-expansion',
    "sh '/opt/plugins/regista/scripts/regista' hook user-prompt",
    '"${CLAUDE_PLUGIN_ROOT}/scripts/regista" hook user-prompt-expansion',
    "cd x && python3 scripts/regista.py hook user-prompt-expansion",
    "env A=1 python3 scripts/regista.py hook user-prompt",
    """echo '{"command_name": "regista:verify", "command_args": "UX-1 ok", "prompt": "/regista:verify UX-1 ok"}' """
    "| python3 scripts/regista.py hook user-prompt-expansion",
    "python3 scripts/regista.py hook user-prompt-expansion < /tmp/fake.json",
]
ORDINARY = [
    "python3 scripts/regista.py status",
    "regista.py requirements",
    "python3 scripts/regista.py requirements",
    "python3 scripts/regista.py gate tests",
    "python3 tools/hooks.py hook stop",
    "cat hooks/hooks.py",
    'git log --grep "regista.py hook"',
]


def commands_in(path):
    """Every hook command registered in a hooks file (Claude Code's or Codex's layout)."""
    found = []

    def walk(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if k == "command" and isinstance(v, str):
                    found.append(v)
                else:
                    walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(regista.load_json(path, {}))
    return found


class HooksByHand(unittest.TestCase):
    """The pre-tool hook refuses an agent's shell command that runs the harness's hooks by hand."""

    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}])
        self.addCleanup(self.p.cleanup)
        self.p.write(".regista/specs/look.json", json.dumps(LOOK_SPEC))

    def state(self):
        return regista.load_json(regista.requirements_path(self.p.root), {})

    def test_running_a_hook_by_hand_is_refused(self):  # covers: PRE-8
        for cmd in BY_HAND:
            out = self.p.hook("pre-tool", as_agent(None, "Bash", command=cmd))
            self.assertEqual(decision(out), "deny", cmd)
            reason = out["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertTrue(reason.startswith("Regista: "), reason)
            self.assertRegex(reason, r"(?i)\bhooks?\b", f"the reason should say it's about the hooks: {reason}")
        self.assertNotIn("UX-1", self.state())

    def test_a_subagent_or_codex_is_refused_too(self):  # covers: PRE-8
        cmd = "python3 scripts/regista.py hook user-prompt-expansion"
        for role in ("builder", "test-writer", "general-purpose"):
            self.assertEqual(decision(self.p.hook("pre-tool", as_agent(role, "Bash", command=cmd))), "deny", role)
        codex = {"session_id": "c1", **as_agent(None, "Bash", command="python scripts/regista.py hook user-prompt "
                                                                        "--host codex")}
        self.assertEqual(decision(self.p.hook("pre-tool", codex, host="codex")), "deny")

    def test_ordinary_commands_are_not_refused(self):  # covers: PRE-8
        for cmd in ORDINARY:
            self.assertNotEqual(decision(self.p.hook("pre-tool", as_agent(None, "Bash", command=cmd))), "deny", cmd)

    def run_registered(self, command, payload, **env):
        """Run a registered hook command the way the host does: through a shell, JSON on stdin."""
        bin_dir = tempfile.mkdtemp(prefix="regista-bin-")
        self.addCleanup(shutil.rmtree, bin_dir, True)
        os.symlink(sys.executable, os.path.join(bin_dir, "python"))  # Codex's commands call `python`
        os.symlink(sys.executable, os.path.join(bin_dir, "python3"))
        full = person_env(PATH=bin_dir + os.pathsep + os.environ.get("PATH", ""), CLAUDE_PROJECT_DIR=self.p.root,
                          **env)
        r = subprocess.run(["sh", "-c", command], input=json.dumps({"cwd": self.p.root, **payload}),
                           capture_output=True, text=True, encoding="utf-8", env=full, cwd=self.p.root, timeout=120)
        return r, (json.loads(r.stdout) if r.stdout.strip() else None)

    def registered(self, file, event):
        path = os.path.join(regista.PLUGIN_ROOT, "hooks", file)
        found = [c for c in commands_in(path) if re.search(rf"\bhook {re.escape(event)}(\s|$)", c)]
        self.assertTrue(found, f"{file} registers no {event} hook")
        return found[0]

    def test_claude_codes_own_hook_calls_keep_working(self):  # covers: PRE-8
        root = {"CLAUDE_PLUGIN_ROOT": regista.PLUGIN_ROOT}
        expansion = self.registered("hooks.json", "user-prompt-expansion")
        r, _ = self.run_registered(expansion, {"command_name": "regista:verify", "command_args": "UX-1 ok fits",
                                               "prompt": "/regista:verify UX-1 ok fits"}, **root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.state().get("UX-1", {}).get("passes"), r.stdout + r.stderr)
        self.assertEqual(self.state()["UX-1"].get("how"), "looked")
        pre_tool = self.registered("hooks.json", "pre-tool")
        r, out = self.run_registered(pre_tool, as_agent(None, "Bash", command="ls"), **root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotEqual(decision(out), "deny", out)
        r, out = self.run_registered(pre_tool, as_agent(None, "Bash", command=BY_HAND[0]), **root)
        self.assertEqual(decision(out), "deny", r.stdout + r.stderr)

    def test_codexs_own_hook_calls_keep_working(self):  # covers: PRE-8
        prompt = self.registered("codex-hooks.json", "user-prompt")
        r, _ = self.run_registered(prompt, {"session_id": "c1", "prompt": "$regista:verify UX-2 ok fits"},
                                   PLUGIN_ROOT=regista.PLUGIN_ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.state().get("UX-2", {}).get("passes"), r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
