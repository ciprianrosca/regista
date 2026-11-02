"""Refusal labels: every refusal or ask the hook gives is logged with its kind, the rule that fired, the role, the
session, the worker and a label (real or unsure), and the user labels the unsure ones with `regista.py refusals`.

Why: 155 refusals in the FPL build (25 to 28 October 2026) were all plain lines without a kind or a label; many of
the reviewer and test-writer ones looked like false alarms, but nothing recorded which.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_refusal_labels -v"""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# test_regista points HOME and CODEX_HOME at a temporary folder when it is imported.
from test_regista import PY, TOOL, Project, as_agent, bash, clean_env, decision, sh, transcript  # noqa: E402

FIELDS = ("kind", "rule", "role", "session", "worker", "label", "label_by")
CERTAIN = ("gate_failed", "protected_path", "no_verify", "push_policy")
LABEL_CMD = f'python3 "{TOOL}" refusals label 1 false_alarm --why "looks fine to me"'


def user_env(**extra):
    """The environment of the user's own terminal: no Claude Code or Codex session variables, no harness ones."""
    env = {k: v for k, v in clean_env().items()
           if not k.startswith(("CLAUDE", "CODEX_")) or k == "CODEX_HOME"}
    env.update(extra)  # after the filter, so a test can put a session variable (CLAUDECODE=1) back on purpose
    return env


def user_cli(project, *args, **extra_env):
    """`regista.py <args>` run from the user's terminal (outside any agent shell)."""
    return subprocess.run([PY, TOOL, *args], cwd=project.root, capture_output=True, text=True, encoding="utf-8",
                          env=user_env(**extra_env))


def log_path(root):
    return os.path.join(root, ".regista", "hooks.log")


def log_bytes(root):
    try:
        with open(log_path(root), "rb") as f:
            return f.read()
    except FileNotFoundError:
        return b""


def refusal_lines(root):
    """The JSON refusal lines of a project's hooks.log, oldest first (other lines are skipped)."""
    rows = []
    for line in log_bytes(root).decode("utf-8").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and "reason" in d:
            rows.append(d)
    return rows


class RefusalLog(unittest.TestCase):
    """What one refusal or ask writes to hooks.log."""

    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo boom; exit 3", "when": ["commit"]}],
                         protected=["secrets/**"])
        self.addCleanup(self.p.cleanup)

    def hook(self, payload, session="s-main"):
        return self.p.hook("pre-tool", {"session_id": session, **payload})

    def last(self):
        rows = refusal_lines(self.p.root)
        self.assertTrue(rows, "nothing was logged to hooks.log")
        return rows[-1]

    def test_a_deny_in_the_main_session_is_logged_with_every_field(self):  # covers: PRE-2
        before = len(refusal_lines(self.p.root))
        self.assertEqual(decision(self.hook(bash("git commit --no-verify -m x"))), "deny")
        rows = refusal_lines(self.p.root)
        self.assertEqual(len(rows), before + 1, "one refusal, one line")
        row = rows[-1]
        for field in FIELDS:
            self.assertIn(field, row, row)
        self.assertEqual((row["kind"], row["rule"], row["role"], row["session"], row["worker"]),
                         ("deny", "no_verify", "main", "s-main", None))
        self.assertEqual((row["label"], row["label_by"]), ("real", "rule"))
        # the fields there were before stay
        self.assertEqual(row["tool"], "Bash")
        self.assertIn("--no-verify", row["input"]["command"])
        self.assertTrue(row["reason"])

    def test_an_ask_in_the_main_session_is_logged_too(self):  # covers: PRE-2
        before = len(refusal_lines(self.p.root))
        self.assertEqual(decision(self.hook(bash("git push origin main"))), "ask")
        rows = refusal_lines(self.p.root)
        self.assertEqual(len(rows), before + 1, "an ask in a main Claude Code session must be logged")
        row = rows[-1]
        self.assertEqual((row["kind"], row["rule"], row["role"], row["session"]), ("ask", "push_policy", "main", "s-main"))
        self.assertEqual((row["label"], row["label_by"]), ("real", "rule"))
        # a different guard that asks: loosening the configuration by hand
        self.assertEqual(decision(self.hook(as_agent(None, "Bash", command="sed -i 's/exit 3/exit 0/' .regista/config.json"))), "ask")
        row = self.last()
        self.assertEqual(row["kind"], "ask")
        self.assertTrue(row["rule"] and isinstance(row["rule"], str), row)
        self.assertIn(row["label"], ("real", "unsure"))
        self.assertEqual(row["label_by"], "rule")

    def test_certain_rules_are_labelled_real(self):  # covers: PRE-2
        cases = {
            "gate_failed": bash("git add -A && git commit -m wip"),
            "no_verify": bash("git commit --no-verify -m x"),
            "protected_path": {"tool_name": "Write",
                               "tool_input": {"file_path": os.path.join(self.p.root, "secrets", "a.key")}},
            "push_policy": bash("git push origin main"),
        }
        for rule, payload in cases.items():
            with self.subTest(rule=rule):
                self.assertIn(decision(self.hook(payload)), ("deny", "ask"))
                row = self.last()
                self.assertEqual(row["rule"], rule, row)
                self.assertEqual((row["label"], row["label_by"]), ("real", "rule"), row)

    def test_a_role_guard_is_labelled_unsure(self):  # covers: PRE-2
        cases = [
            ("test-writer", as_agent("test-writer", "Bash", command="cat src/a.py")),
            ("reviewer", as_agent("reviewer", "Bash", command="echo x > src/a.py")),
            ("builder", as_agent("builder", "Edit", file_path=os.path.join(self.p.root, "tests", "test_a.py"))),
            ("researcher", as_agent("researcher", "Write", file_path=os.path.join(self.p.root, "src", "a.py"))),
            ("main", as_agent(None, "Write", file_path=os.path.join(self.p.root, "src", "a.py"))),
        ]
        for role, payload in cases:
            with self.subTest(role=role):
                self.assertEqual(decision(self.hook(payload, session="s-agents")), "deny")
                row = self.last()
                self.assertEqual((row["kind"], row["rule"], row["role"]), ("deny", "role_guard", role), row)
                self.assertEqual((row["session"], row["worker"]), ("s-agents", None), row)
                self.assertEqual((row["label"], row["label_by"]), ("unsure", "rule"), row)

    def test_calls_let_through_log_nothing(self):  # covers: PRE-2
        before = log_bytes(self.p.root)
        self.assertIsNone(decision(self.hook(as_agent("test-writer", "Bash", command="python -m pytest tests/test_a.py"))))
        self.assertIsNone(decision(self.hook(as_agent(None, "Read", file_path=os.path.join(self.p.root, "src", "a.py")))))
        self.assertEqual(log_bytes(self.p.root), before)

    def test_path_rules_matched_through_a_folder_are_unsure(self):  # covers: PRE-2
        """A command that names a folder holding a guarded path (".", ".regista/") may be refused, but the match
        isn't exact, so the refusal waits for the user's label instead of counting as real."""
        commands = ["find . -name '*.pyc' -delete", "git restore .", "git checkout -- .", "mv /tmp/x.md .regista/"]
        for cmd in commands:
            with self.subTest(cmd=cmd):
                before = len(refusal_lines(self.p.root))
                answer = decision(self.hook(bash(cmd)))
                new = refusal_lines(self.p.root)[before:]
                for row in new:
                    self.assertNotEqual(row.get("label"), "real", row)
                if answer in ("deny", "ask"):
                    self.assertTrue(new, f"the refusal of {cmd!r} wasn't logged")
                    self.assertEqual((new[-1]["label"], new[-1]["label_by"]), ("unsure", "rule"), new[-1])

    def test_an_exact_write_to_the_config_is_real(self):  # covers: PRE-2
        config = os.path.join(self.p.root, ".regista", "config.json")
        for tool in ("Write", "Edit"):
            with self.subTest(tool=tool):
                before = len(refusal_lines(self.p.root))
                answer = decision(self.hook(as_agent(None, tool, file_path=config)))
                new = refusal_lines(self.p.root)[before:]
                if answer in ("deny", "ask"):
                    self.assertEqual(len(new), 1, new)
                    self.assertEqual((new[0]["label"], new[0]["label_by"]), ("real", "rule"), new[0])
                else:
                    self.assertEqual(new, [])

    def test_a_context_limit_refusal_is_unsure(self):  # covers: PRE-2
        p = Project(gates=[{"name": "tests", "run": "echo ok"}], context={"on_limit": "pause"})
        self.addCleanup(p.cleanup)
        t = os.path.join(p.root, "..", os.path.basename(p.root) + "-transcript.jsonl")
        self.addCleanup(lambda: os.path.exists(t) and os.remove(t))
        transcript(t, 132_000)  # 66 % of a 200k window: over the 65 % limit
        out = p.hook("pre-tool", {"transcript_path": t, "session_id": "s-full", **bash("pytest -q")})
        self.assertEqual(decision(out), "deny")
        row = refusal_lines(p.root)[-1]
        self.assertEqual((row["kind"], row["rule"], row["session"]), ("deny", "context_limit", "s-full"), row)
        self.assertEqual((row["label"], row["label_by"]), ("unsure", "rule"), row)

    def test_a_logging_error_still_returns_the_refusal(self):  # covers: PRE-2
        path = log_path(self.p.root)
        if os.path.isfile(path):
            os.remove(path)
        os.makedirs(path)  # hooks.log is a folder: appending to it fails
        self.assertEqual(decision(self.hook(bash("git commit --no-verify -m x"))), "deny")
        self.assertEqual(decision(self.hook(bash("git push origin main"))), "ask")
        self.assertEqual(decision(self.hook(as_agent("test-writer", "Bash", command="cat src/a.py"))), "deny")


class RefusalsCommand(unittest.TestCase):
    """`regista.py refusals`: the listing, `--unlabelled`, and the user's labels."""

    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo boom; exit 3", "when": ["commit"]}],
                         protected=["secrets/**"])
        self.addCleanup(self.p.cleanup)

    def hook(self, payload):
        return self.p.hook("pre-tool", {"session_id": "s1", **payload})

    def make_refusals(self):
        """Three certain refusals and two unsure ones (both role guards, likely within the same second)."""
        self.hook(bash("git commit --no-verify -m x"))
        self.hook(bash("git add -A && git commit -m wip"))
        self.hook(as_agent("test-writer", "Bash", command="cat src/a.py"))
        self.hook(bash("git push origin main"))
        self.hook(as_agent("reviewer", "Bash", command="echo x > src/a.py"))

    def listing(self, *args):
        r = self.p.cli("refusals", "--json", *args)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rows = json.loads(r.stdout)
        self.assertIsInstance(rows, list)
        return rows

    def text(self, *args):
        r = self.p.cli("refusals", *args)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def by_role(self, rows, role):
        found = [x for x in rows if x.get("role") == role]
        self.assertEqual(len(found), 1, rows)
        return found[0]

    def test_listing_shows_every_refusal_newest_first(self):  # covers: PRE-2
        self.make_refusals()
        rows = self.listing()
        self.assertEqual(len(rows), len(refusal_lines(self.p.root)))
        self.assertEqual([x["rule"] for x in rows],
                         ["role_guard", "push_policy", "role_guard", "gate_failed", "no_verify"])
        self.assertEqual(len({x["number"] for x in rows}), len(rows), "each refusal has its own number")
        out = self.text()
        for word in ("no_verify", "gate_failed", "push_policy", "role_guard", "test-writer", "reviewer",
                     "real", "unsure"):
            self.assertIn(word, out)

    def test_unlabelled_lists_exactly_the_unsure_ones(self):  # covers: PRE-2
        self.make_refusals()
        out = self.text("--unlabelled")
        for rule in ("no_verify", "gate_failed", "push_policy"):
            self.assertNotIn(rule, out, "a certain refusal isn't waiting for a label")
        lines = [ln for ln in out.splitlines() if "role_guard" in ln]
        self.assertEqual(len(lines), 2, out)
        self.assertTrue(any("test-writer" in ln for ln in lines) and any("reviewer" in ln for ln in lines), out)

    def test_unlabelled_with_nothing_unsure(self):  # covers: PRE-2
        self.hook(bash("git commit --no-verify -m x"))
        r = self.p.cli("refusals", "--unlabelled")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("no_verify", r.stdout)

    def test_no_refusals_at_all(self):  # covers: PRE-2
        self.assertEqual(self.listing(), [])
        self.assertEqual(self.p.cli("refusals").returncode, 0)

    def test_the_user_labels_a_false_alarm_without_rewriting_the_log(self):  # covers: PRE-2
        self.make_refusals()
        writer = self.by_role(self.listing(), "test-writer")
        before = log_bytes(self.p.root)
        r = user_cli(self.p, "refusals", "label", str(writer["number"]), "false_alarm", "--why", "cat of a test fixture")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = log_bytes(self.p.root)
        self.assertTrue(after.startswith(before), "hooks.log may only grow: its old lines stay byte for byte")

        rows = self.listing()
        writer = self.by_role(rows, "test-writer")
        self.assertEqual((writer["label"], writer["label_by"], writer["label_why"]),
                         ("false_alarm", "user", "cat of a test fixture"))
        # the other role guard, logged the same second, keeps its label
        reviewer = self.by_role(rows, "reviewer")
        self.assertEqual((reviewer["label"], reviewer["label_by"]), ("unsure", "rule"))
        self.assertIn("false_alarm", self.text())
        unlabelled = [ln for ln in self.text("--unlabelled").splitlines() if "role_guard" in ln]
        self.assertEqual(len(unlabelled), 1, unlabelled)
        self.assertIn("reviewer", unlabelled[0])

        # the user can also confirm a refusal as real, and relabel a certain one
        r = user_cli(self.p, "refusals", "label", str(reviewer["number"]), "real", "--why", "it did write to src")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        reviewer = self.by_role(self.listing(), "reviewer")
        self.assertEqual((reviewer["label"], reviewer["label_by"], reviewer["label_why"]),
                         ("real", "user", "it did write to src"))
        self.assertEqual([ln for ln in self.text("--unlabelled").splitlines() if "role_guard" in ln], [])
        self.assertTrue(log_bytes(self.p.root).startswith(before))

    def test_a_label_survives_new_refusals(self):  # covers: PRE-2
        self.make_refusals()
        writer = self.by_role(self.listing(), "test-writer")
        self.assertEqual(user_cli(self.p, "refusals", "label", str(writer["number"]), "false_alarm", "--why", "x").returncode, 0)
        self.hook(as_agent("researcher", "Write", file_path=os.path.join(self.p.root, "src", "a.py")))
        rows = self.listing()
        self.assertEqual(rows[0]["role"], "researcher")
        self.assertEqual(self.by_role(rows, "test-writer")["label"], "false_alarm")

    def test_wrong_label_or_number_is_refused_and_changes_nothing(self):  # covers: PRE-2
        self.make_refusals()
        rows = self.listing()
        before = log_bytes(self.p.root)
        missing = max(x["number"] for x in rows) + 100
        for args in ((str(rows[0]["number"]), "maybe"), (str(missing), "false_alarm"), ("abc", "real")):
            with self.subTest(args=args):
                r = user_cli(self.p, "refusals", "label", *args, "--why", "x")
                self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual(log_bytes(self.p.root), before)
        self.assertEqual(self.listing(), rows)

    def test_old_lines_without_the_new_fields_still_list(self):  # covers: PRE-2
        old = {"at": "2026-10-25T18:24:59", "tool": "Bash",
               "input": {"command": "sed -n 262,320p src/poller/poll.js"},
               "reason": "Daedalus: the reviewer doesn't change project files from the shell."}
        os.makedirs(os.path.dirname(log_path(self.p.root)), exist_ok=True)
        with open(log_path(self.p.root), "a", encoding="utf-8") as f:
            f.write("2026-10-25 session start\n")  # older logs mix in plain text lines
            f.write(json.dumps(old) + "\n")
        self.hook(bash("git commit --no-verify -m x"))
        rows = self.listing()
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual(rows[0]["rule"], "no_verify")
        self.assertEqual(rows[1]["reason"], old["reason"])
        self.assertEqual(rows[1]["at"], old["at"])
        self.assertIn("reviewer doesn't change project files", self.text())
        before = log_bytes(self.p.root)
        r = user_cli(self.p, "refusals", "label", str(rows[1]["number"]), "false_alarm", "--why", "sed -n only reads")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(log_bytes(self.p.root).startswith(before))
        labelled = [x for x in self.listing() if x["at"] == old["at"]]
        self.assertEqual((labelled[0]["label"], labelled[0]["label_by"]), ("false_alarm", "user"))


class AgentsMayNotLabel(unittest.TestCase):
    """Labels are the user's call: an agent running `refusals label` is refused, like verify-manual."""

    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}])
        self.addCleanup(self.p.cleanup)

    def test_each_agent_role_is_refused_and_the_refusal_is_real(self):  # covers: PRE-5
        for role in ("builder", "test-writer", "reviewer", "researcher"):
            with self.subTest(role=role):
                out = self.p.hook("pre-tool", {"session_id": "s1", **as_agent(role, "Bash", command=LABEL_CMD)})
                self.assertEqual(decision(out), "deny")
                row = refusal_lines(self.p.root)[-1]
                self.assertIn("refusals label", row["input"]["command"])
                self.assertEqual((row["kind"], row["role"]), ("deny", role), row)
                self.assertEqual((row["label"], row["label_by"]), ("real", "rule"), row)

    def test_the_refused_label_never_lands(self):  # covers: PRE-5
        self.p.hook("pre-tool", {"session_id": "s1", **as_agent("test-writer", "Bash", command="cat src/a.py")})
        self.p.hook("pre-tool", {"session_id": "s1", **as_agent("builder", "Bash", command=LABEL_CMD)})
        r = self.p.cli("refusals", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rows = json.loads(r.stdout)
        self.assertEqual(rows[0]["role"], "builder")
        self.assertEqual(rows[0]["label"], "real")
        writer = [x for x in rows if x["role"] == "test-writer"]
        self.assertEqual(writer[0]["label"], "unsure")

    def test_a_worker_session_is_refused_too(self):  # covers: PRE-5, PRE-2
        root = self.p.root
        with open(os.path.join(root, ".git", "info", "exclude"), "a", encoding="utf-8") as f:
            f.write("# Regista workers\n.regista/worktrees/\n")
        wt = os.path.join(root, ".regista", "worktrees", "m2")
        sh(root, "git", "worktree", "add", "-q", wt, "-b", "regista/m2")
        self.addCleanup(sh, root, "git", "worktree", "prune")
        self.addCleanup(shutil.rmtree, wt, True)
        with open(os.path.join(wt, ".regista", "worker.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "m2", "milestone": "M2", "slot": 1, "overseer_root": root, "branch": "regista/m2"}, f)
        out = self.p.hook("pre-tool", {"cwd": wt, "session_id": "w1", **bash(LABEL_CMD)},
                          extra_env={"REGISTA_WORKER": "m2", "REGISTA_SLOT": "1"})
        self.assertEqual(decision(out), "deny")
        rows = [x for x in refusal_lines(wt) + refusal_lines(root) if "refusals label" in str(x.get("input"))]
        self.assertEqual(len(rows), 1, "the worker's refusal is logged once")
        row = rows[0]
        self.assertEqual((row["kind"], row["worker"], row["session"]), ("deny", "m2", "w1"), row)
        self.assertEqual((row["label"], row["label_by"]), ("real", "rule"), row)

    def test_the_main_session_is_refused_too(self):  # covers: PRE-5
        cmd = f'python3 "{TOOL}" refusals label 1 false_alarm --why x'
        out = self.p.hook("pre-tool", {"session_id": "s-main", **bash(cmd)})
        self.assertEqual(decision(out), "deny", out)
        rows = [x for x in refusal_lines(self.p.root) if "refusals label" in str(x.get("input"))]
        self.assertEqual(len(rows), 1, "the main session's refusal is logged once")
        row = rows[0]
        self.assertEqual((row["kind"], row["rule"], row["session"]), ("deny", "label_by_agent", "s-main"), row)
        self.assertEqual((row["label"], row["label_by"]), ("real", "rule"), row)

    def test_the_command_itself_refuses_inside_a_claude_code_shell(self):  # covers: PRE-5
        """The second defence: even if a route around the hook's parsing reaches the command, it won't label
        from a Claude Code tool shell; from the user's own terminal it does."""
        self.p.hook("pre-tool", {"session_id": "s1", **as_agent("test-writer", "Bash", command="cat src/a.py")})
        listing = user_cli(self.p, "refusals", "--json")
        self.assertEqual(listing.returncode, 0, listing.stdout + listing.stderr)
        rows = json.loads(listing.stdout)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["label"], "unsure")
        number = str(rows[0]["number"])
        before = log_bytes(self.p.root)

        r = user_cli(self.p, "refusals", "label", number, "false_alarm", "--why", "x", CLAUDECODE="1")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(log_bytes(self.p.root), before)
        self.assertEqual(json.loads(user_cli(self.p, "refusals", "--json").stdout), rows)

        r = user_cli(self.p, "refusals", "label", number, "false_alarm", "--why", "x")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        row = json.loads(user_cli(self.p, "refusals", "--json").stdout)[0]
        self.assertEqual((row["label"], row["label_by"], row["label_why"]), ("false_alarm", "user", "x"))


if __name__ == "__main__":
    unittest.main()
