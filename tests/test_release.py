"""Gates that run only when certain files change, long checks run elsewhere, and releases. Klotho's planner did
these by hand before Regista had a place for them: the ruler checks only for ruler changes, an hour-long check on
the staging server, and a tagged release with its notes and a deploy of the tag."""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_regista import PY, TOOL, Project, as_agent, bash, decision, regista, sh  # noqa: E402

TODAY = dt.date.today().isoformat()


def text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def cli(p, *args, env=None):
    return subprocess.run([PY, TOOL, *args], cwd=p.root, capture_output=True, text=True, encoding="utf-8",
                          env={**os.environ, **(env or {})})


def set_config(p, **changes):
    path = os.path.join(p.root, ".regista", "config.json")
    with open(path, encoding="utf-8") as f:
        c = json.load(f)
    c.update(changes)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(c, f)
    sh(p.root, "git", "add", "-A")
    sh(p.root, "git", "commit", "-q", "-m", "config")


class WhenChanged(unittest.TestCase):
    """A gate with when_changed runs only for changes that touch its paths, and never skips one it should run."""

    def tearDown(self):
        self.p.cleanup()

    def test_commit_runs_it_only_for_matching_changes(self):
        self.p = Project(gates=[{"name": "rulers", "run": "exit 1", "when_changed": ["src/ruler/**"]}])
        self.p.write("src/ui/page.py", "x = 1\n")
        self.assertIsNone(self.p.hook("pre-bash", bash("git commit -am ui")))
        self.p.write("src/ruler/read.py", "x = 1\n")
        out = self.p.hook("pre-bash", bash("git commit -am ruler"))
        self.assertEqual(decision(out), "deny")

    def test_skipped_gate_says_why_in_plain_words(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"},
                                {"name": "rulers", "run": "exit 1", "when_changed": ["src/ruler/**"]}])
        cfg = regista.load_config(self.p.root)
        res = regista.run_gates(self.p.root, cfg, regista.select_gates(cfg, event="commit", files=["src/ui/a.py"]))
        text = regista.format_results(res)
        self.assertIn("rulers: skipped (not needed for this change", text)
        self.assertIn("tests: PASS", text)

    def test_unknown_or_empty_change_set_runs_it(self):
        self.p = Project()
        cfg = {"gates": [{"name": "rulers", "run": "true", "when_changed": ["src/ruler/**"]}]}
        for files in (None, []):
            self.assertFalse(regista.select_gates(cfg, event="commit", files=files)[0].get("_skip"), files)
        self.assertTrue(regista.select_gates(cfg, event="commit", files=["docs/a.md"])[0].get("_skip"))

    def test_a_skipped_end_to_end_gate_keeps_its_requirements_result(self):
        from test_regista import E2E_GATE, SPEC
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}, {**E2E_GATE, "when_changed": ["src/cli/**"]}])
        self.p.write(".regista/specs/stats.json", json.dumps(SPEC))
        self.p.write("tests/test_stats.py", "# covers: STATS-1\n")
        self.p.write("e2e/cli_test.sh", "# covers: STATS-2\n")
        self.p.write("src/cli/main.py", "x = 1\n")
        commit = lambda: self.p.hook("pre-tool", as_agent(None, "Bash", command="git add -A && git commit -qm x"))  # noqa: E731
        self.assertIsNone(decision(commit()))
        sh(self.p.root, "git", "add", "-A")
        sh(self.p.root, "git", "commit", "-q", "-m", "cli")
        state = lambda: regista.load_json(regista.requirements_path(self.p.root), {})  # noqa: E731
        self.assertTrue(state()["STATS-2"]["passes"])
        self.p.write("src/ui.py", "y = 1\n")
        self.assertIsNone(decision(commit()))
        self.assertTrue(state()["STATS-2"]["passes"], "the end-to-end gate wasn't needed: its result stands")

    def test_paths_is_the_older_name(self):
        self.p = Project()
        cfg = {"gates": [{"name": "geo", "run": "true", "paths": ["src/geo/**"]}]}
        self.assertTrue(regista.select_gates(cfg, event="commit", files=["docs/a.md"])[0].get("_skip"))

    def test_a_rename_out_of_its_paths_counts(self):
        self.p = Project()
        self.p.commit("src/ruler/a.py", "x = 1\n")
        sh(self.p.root, "git", "mv", "src/ruler/a.py", "src/a.py")
        sh(self.p.root, "git", "commit", "-q", "-m", "move")
        self.assertIn("src/ruler/a.py", regista.diff_files(self.p.root, "HEAD~1"))
        self.assertIsNone(regista.diff_files(self.p.root, "no-such-commit"))

    def test_deploy_checks_run_for_what_changed_since_the_last_good_deploy(self):
        live = tempfile.mkdtemp(prefix="regista-live-")
        self.addCleanup(shutil.rmtree, live, True)
        count = os.path.join(live, "count")
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"},
                                {"name": "ruler-live", "run": f"echo x >> {count}", "when": ["deploy"],
                                 "when_changed": ["src/ruler/**"]}],
                         deploy={"run": "true"})
        runs = lambda: len(text(count).split()) if os.path.exists(count) else 0  # noqa: E731
        self.assertEqual(cli(self.p, "deploy").returncode, 0)
        self.assertEqual(runs(), 1, "the first deploy has nothing to compare with: it runs")
        self.p.commit("src/ui/a.py", "x = 1\n")
        r = cli(self.p, "deploy")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(runs(), 1)
        self.assertIn("not needed for this change", r.stdout)
        self.p.commit("src/ruler/a.py", "x = 1\n")
        cli(self.p, "deploy")
        self.assertEqual(runs(), 2)
        cli(self.p, "deploy")
        self.assertEqual(runs(), 3, "the same commit again: an empty change set runs it")


class LongChecks(unittest.TestCase):
    """A check that runs for an hour on another server: started, recorded against a commit, collected later."""

    def setUp(self):
        self.box = tempfile.mkdtemp(prefix="regista-box-")
        self.status = os.path.join(self.box, "status")
        self.gate = {"name": "real", "kind": "detached", "when": ["milestone", "release"],
                     "start": f"echo {{sha}} {{label}} > {self.box}/started && echo running > {self.status}",
                     "status": f"cat {self.status}"}
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}, self.gate])
        self.p.commit("src/a.py", "x = 1\n")

    def tearDown(self):
        self.p.cleanup()
        shutil.rmtree(self.box, ignore_errors=True)

    def say(self, text):
        with open(self.status, "w") as f:
            f.write(text)

    def result(self):
        cfg = regista.load_config(self.p.root)
        return regista.detached_result(self.p.root, cfg, self.gate)

    def test_start_collect_and_a_pass_on_this_code(self):
        r = cli(self.p, "gate", "real", "--start")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        head = sh(self.p.root, "git", "rev-parse", "HEAD").strip()
        self.assertTrue(text(os.path.join(self.box, "started")).startswith(head + " real-" + head[:7]))
        self.assertIn("still running", cli(self.p, "gate", "--collect").stdout)
        self.assertEqual(self.result()["status"], "fail")
        self.assertIn("still running", self.result()["fails"][0])
        self.say("all done\npass\n")
        self.assertIn("real on", cli(self.p, "gate", "--collect").stdout)
        self.assertEqual(self.result()["status"], "pass")
        again = cli(self.p, "gate", "real", "--start")
        self.assertIn("already passed", again.stdout)

    def test_commits_never_wait_for_it(self):
        self.gate["when"] = ["commit", "release"]
        self.p = Project(gates=[self.gate])
        self.p.write("src/b.py", "y = 2\n")
        self.assertIsNone(self.p.hook("pre-bash", bash("git commit -am b")))
        cfg = regista.load_config(self.p.root)
        self.assertIn("runs elsewhere", regista.select_gates(cfg, event="commit", files=["src/b.py"])[0]["_skip"])

    def test_uncommitted_files_arent_part_of_it(self):
        self.p.write("src/b.py", "y = 2\n")
        r = cli(self.p, "gate", "real", "--start")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("It checks the commit only: 1 uncommitted file here isn't part of it (src/b.py)", r.stdout)
        self.say("pass")
        cli(self.p, "gate", "--collect")
        self.assertEqual(self.result()["status"], "fail", "the pass is for the commit, not for src/b.py")

    def test_a_pass_holds_for_the_same_code_only(self):
        cli(self.p, "gate", "real", "--start")
        self.say("pass")
        cli(self.p, "gate", "--collect")
        self.p.commit(".regista/status.md", "# Status\n", "status")  # the harness's records: same code
        self.assertEqual(self.result()["status"], "pass")
        self.p.commit("src/a.py", "x = 2\n", "code")
        self.assertEqual(self.result()["status"], "fail")
        self.assertIn("no passing run on this code", self.result()["fails"][0])

    def test_with_when_changed_a_pass_holds_until_its_paths_change(self):
        self.gate["when_changed"] = ["src/ruler/**"]
        set_config(self.p, gates=[self.gate])
        cli(self.p, "gate", "real", "--start")
        self.say("pass")
        cli(self.p, "gate", "--collect")
        self.p.commit("src/ui.py", "x = 2\n", "ui")
        self.assertEqual(self.result()["status"], "pass")
        self.p.commit("src/ruler/r.py", "x = 2\n", "ruler")
        self.assertEqual(self.result()["status"], "fail")

    def test_a_changed_start_command_voids_the_pass(self):
        cli(self.p, "gate", "real", "--start")
        self.say("pass")
        cli(self.p, "gate", "--collect")
        self.gate["start"] += " # other"
        self.assertEqual(self.result()["status"], "fail")

    def test_a_failure_and_metrics_over_their_limit(self):
        cli(self.p, "gate", "real", "--start")
        self.say("fail")
        self.assertIn("FAILED", cli(self.p, "gate", "--collect").stdout)
        self.assertIn("failed", self.result()["fails"][0])
        self.gate["metrics"] = {"p95_mm": {"max": 1.5}}
        set_config(self.p, gates=[self.gate])
        cli(self.p, "gate", "real", "--start")
        self.say('{"p95_mm": 1.9}')
        out = cli(self.p, "gate", "--collect").stdout
        self.assertIn("FAILED", out)
        self.assertIn("> max 1.5", out)

    def test_status_it_cant_read_keeps_it_open(self):
        cli(self.p, "gate", "real", "--start")
        self.say("ssh: connect to host staging: Connection refused")
        out = cli(self.p, "gate", "--collect").stdout
        self.assertIn("couldn't tell how it's doing", out)
        self.say("pass")
        self.assertIn("PASSED", cli(self.p, "gate", "--collect").stdout)

    def test_a_run_past_its_time_limit_fails(self):
        cli(self.p, "gate", "real", "--start")
        path = os.path.join(self.p.root, ".regista", "gates", "detached.json")
        data = json.loads(text(path))
        data["runs"][-1]["started"] = (dt.datetime.now() - dt.timedelta(hours=7)).isoformat(timespec="seconds")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.assertIn("time limit", cli(self.p, "gate", "--collect").stdout)

    def test_milestone_waits_for_it_and_session_start_collects(self):
        cfg = regista.load_config(self.p.root)
        self.assertTrue(any("real" in x for x in regista.long_check_reasons(self.p.root, cfg, "milestone")))
        cli(self.p, "gate", "real", "--start")
        self.say("pass")
        out = self.p.hook("session-start", {"source": "startup"})
        self.assertIn("real on", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(regista.long_check_reasons(self.p.root, cfg, "milestone"), [])

    def test_agents_cant_write_its_records(self):
        out = self.p.hook("pre-tool", as_agent(None, "Write", file_path=os.path.join(
            self.p.root, ".regista", "gates", "detached.json"), content="{}"))
        self.assertEqual(decision(out), "deny")


GH = r'''#!PYTHON
import os, sys
with open(os.environ["GH_LOG"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n---\n" + sys.stdin.read() + "\n===\n")
'''


class Releases(unittest.TestCase):
    def setUp(self):
        self.box = tempfile.mkdtemp(prefix="regista-rel-")
        self.remote = os.path.join(self.box, "origin.git")
        sh(self.box, "git", "init", "-q", "--bare", self.remote)
        self.deployed = os.path.join(self.box, "deployed")
        self.script = os.path.join(self.box, "deploy.sh")
        with open(self.script, "w") as f:
            f.write(f'echo "$1" > {self.deployed}\n')
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}], push="allow",
                         release={"version_files": ["pyproject.toml", "uv.lock"], "heading": "## {version} — {date}",
                                  "github_release": True},
                         deploy={"run": f"sh {self.script} {{ref}}", "ref": "tag"})
        self.p.write("pyproject.toml", '[project]\nname = "demo_app"\nversion = "0.5.2"\n')
        self.p.write("uv.lock", 'version = 1\n\n[[package]]\nname = "other"\nversion = "9.9.9"\n\n'
                                '[[package]]\nname = "demo-app"\nversion = "0.5.2"\n')
        self.p.commit("CHANGELOG.md", "# Changelog\n\n## 0.5.2 — 2026-10-01\n\nEarlier.\n", "0.5.2")
        sh(self.p.root, "git", "tag", "-a", "v0.5.2", "-m", "0.5.2")
        sh(self.p.root, "git", "remote", "add", "origin", self.remote)
        sh(self.p.root, "git", "push", "-q", "-u", "origin", "main")
        self.gh = os.path.join(self.box, "gh")
        with open(self.gh, "w") as f:
            f.write(GH.replace("PYTHON", PY))
        os.chmod(self.gh, 0o755)
        self.env = {"REGISTA_GH": self.gh, "GH_LOG": os.path.join(self.box, "gh.log")}

    def tearDown(self):
        self.p.cleanup()
        shutil.rmtree(self.box, ignore_errors=True)

    def changelog(self, version, date=TODAY, notes="Clearer messages."):
        self.p.write("CHANGELOG.md", f"# Changelog\n\n## {version} — {date}\n\n{notes}\n\n## 0.5.2 — 2026-10-01\n\nEarlier.\n")

    def test_says_plainly_whats_missing_and_changes_nothing(self):
        self.p.write("src/a.py", "x = 1\n")
        r = cli(self.p, "release")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Not released: 0.5.3 is not ready. What's missing:", r.stdout)
        self.assertIn(f'CHANGELOG.md has no entry for 0.5.3. Add one at the top, headed "## 0.5.3 — {TODAY}"', r.stdout)
        self.assertIn("1 file isn't committed (src/a.py)", r.stdout)
        self.changelog("0.5.3", date="2026-10-27")
        self.assertIn(f"dated 2026-10-27. A release is dated the day it's tagged: change it to {TODAY}",
                      cli(self.p, "release").stdout)
        self.assertIn('version = "0.5.2"', text(os.path.join(self.p.root, "pyproject.toml")))
        self.assertEqual(sh(self.p.root, "git", "tag").split(), ["v0.5.2"])

    def test_patch_release_bumps_tags_pushes_publishes_and_deploys_the_tag(self):
        self.changelog("0.5.3")
        r = cli(self.p, "release", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Released 0.5.3, a patch release after 0.5.2.", r.stdout)
        self.assertIn('version = "0.5.3"', text(os.path.join(self.p.root, "pyproject.toml")))
        lock = text(os.path.join(self.p.root, "uv.lock"))
        self.assertIn('name = "demo-app"\nversion = "0.5.3"', lock)
        self.assertIn('name = "other"\nversion = "9.9.9"', lock)
        # the release commit is tagged; the deploy then commits its own record on top (lesson 3)
        self.assertEqual(sh(self.p.root, "git", "log", "-1", "--format=%s", "v0.5.3").strip(), "Release 0.5.3")
        self.assertTrue(sh(self.p.root, "git", "log", "-1", "--format=%s").startswith("Record the deploy"))
        self.assertEqual(sh(self.p.root, "git", "status", "--porcelain", "--untracked-files=no").strip(), "")
        self.assertIn("v0.5.3", sh(self.remote, "git", "tag"))
        self.assertEqual(sh(self.remote, "git", "rev-parse", "main"), sh(self.p.root, "git", "rev-parse", "v0.5.3^{commit}"))
        log = text(self.env["GH_LOG"])
        self.assertIn("release create v0.5.3 --verify-tag --title Demo 0.5.3", log)
        self.assertIn("Clearer messages.", log)
        self.assertNotIn("Earlier.", log)
        self.assertEqual(text(self.deployed).strip(), "v0.5.3")
        self.assertIn("Live: v0.5.3", cli(self.p, "status").stdout)

    def test_version_set_ahead_by_a_pull_request_is_released_as_it_is(self):
        self.p.write("pyproject.toml", '[project]\nname = "demo_app"\nversion = "0.5.3"\n')
        self.p.write("uv.lock", '[[package]]\nname = "demo-app"\nversion = "0.5.3"\n')
        self.changelog("0.5.3")
        sh(self.p.root, "git", "add", "-A")
        sh(self.p.root, "git", "commit", "-q", "-m", "PR #77")
        head = sh(self.p.root, "git", "rev-parse", "HEAD")
        self.assertIn("asked for a minor release", cli(self.p, "release", "minor", "--no-push").stdout)
        r = cli(self.p, "release", "--no-push")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(sh(self.p.root, "git", "rev-parse", "v0.5.3^{commit}"), head, "no bump commit")
        self.assertIn("so the rest is yours to do", r.stdout)
        self.assertIn("git push origin v0.5.3", r.stdout)
        self.assertNotIn("v0.5.3", sh(self.remote, "git", "tag"))

    def test_a_failing_gate_puts_the_version_back(self):
        set_config(self.p, gates=[{"name": "tests", "run": "grep -q 0.5.2 pyproject.toml"}])
        self.changelog("0.5.3")
        r = cli(self.p, "release")
        self.assertEqual(r.returncode, 1)
        self.assertIn("a gate fails on 0.5.3, so the version files are back as they were", r.stdout)
        self.assertIn('version = "0.5.2"', text(os.path.join(self.p.root, "pyproject.toml")))
        self.assertIn("## 0.5.3", text(os.path.join(self.p.root, "CHANGELOG.md")), "the user's notes stay")

    def test_gates_judged_against_the_last_release(self):
        set_config(self.p, gates=[{"name": "tests", "run": "echo ok"},
                                  {"name": "rulers", "run": "exit 1", "when": ["release"], "when_changed": ["src/ruler/**"]}])
        self.p.commit("src/ui.py", "x = 1\n")
        self.changelog("0.5.3")
        r = cli(self.p, "release", "--no-push")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("rulers: skipped (not needed for this change", r.stdout)
        self.p.commit("src/ruler/r.py", "x = 1\n")
        self.changelog("0.5.4")
        r = cli(self.p, "release", "--no-push")
        self.assertEqual(r.returncode, 1)
        self.assertIn("rulers: FAIL", r.stdout)

    def test_release_waits_for_a_long_check_on_this_code(self):
        status = os.path.join(self.box, "status")
        set_config(self.p, gates=[{"name": "real", "kind": "detached", "when": ["release"],
                                   "start": f"echo running > {status}", "status": f"cat {status}"}])
        self.changelog("0.5.3")
        r = cli(self.p, "release", "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("a long check has no pass on this code yet", r.stdout)
        cli(self.p, "gate", "real", "--start")
        with open(status, "w") as f:
            f.write("pass")
        cli(self.p, "gate", "--collect")
        r = cli(self.p, "release", "--no-push")  # the uncommitted changelog and the bump don't void the pass
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("real: PASS, run elsewhere", r.stdout)

    def test_push_block_keeps_it_local(self):
        set_config(self.p, push="block")
        self.changelog("0.5.3")
        r = cli(self.p, "release", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("Pushing is off for this project", r.stdout)
        self.assertNotIn("v0.5.3", sh(self.remote, "git", "tag"))
        self.assertFalse(os.path.exists(self.deployed))

    def test_agents_need_the_user_for_bigger_releases_and_pushes(self):
        self.changelog("0.5.3")
        cmd = f'python "{TOOL}" release'
        self.assertIsNone(decision(self.p.hook("pre-tool", as_agent(None, "Bash", command=cmd))), "patch, push allowed")
        self.assertIsNone(decision(self.p.hook("pre-tool", as_agent(None, "Bash", command=cmd + " minor"))),
                          "it would change nothing: the changelog has no 0.6.0 entry")
        self.changelog("0.6.0")
        out = self.p.hook("pre-tool", as_agent(None, "Bash", command=cmd + " minor"))
        self.assertEqual(decision(out), "ask")
        self.assertIn("only the user decides a release above a patch",
                      out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(decision(self.p.hook("pre-tool", as_agent(None, "Bash", command=cmd + " minor --check"))))
        self.assertEqual(decision(self.p.hook("pre-tool", as_agent("builder", "Bash", command=cmd))), "deny")
        set_config(self.p, push="ask")
        self.changelog("0.5.3")
        out = self.p.hook("pre-tool", as_agent(None, "Bash", command=cmd))
        self.assertEqual(decision(out), "ask")
        self.assertIn("pushes the tag v0.5.3", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(decision(self.p.hook("pre-tool", as_agent(None, "Bash", command=cmd + " --no-push"))))

    def test_publishing_on_github_follows_the_push_policy(self):
        set_config(self.p, push="ask")
        for cmd in ("gh pr merge 77 -R owner/app --merge", "gh release create v0.5.3 --notes-file -"):
            self.assertEqual(decision(self.p.hook("pre-tool", as_agent(None, "Bash", command=cmd))), "ask", cmd)
        self.assertIsNone(decision(self.p.hook("pre-tool", as_agent(None, "Bash", command="gh pr view 77"))))
        set_config(self.p, push="block")
        self.assertEqual(decision(self.p.hook("pre-tool", as_agent(None, "Bash", command="gh pr merge 77"))), "deny")

    def test_codex_asks_in_the_chat(self):
        self.changelog("1.0.0")
        out = self.p.hook("pre-tool", as_agent(None, "Bash", command=f'python "{TOOL}" release major'), host="codex")
        self.assertEqual(decision(out), "deny")
        self.assertIn("Ask the user", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_tagged_deploys_only(self):
        self.p.commit("src/a.py", "x = 1\n")
        r = cli(self.p, "deploy")
        self.assertEqual(r.returncode, 1)
        self.assertIn("deploys tagged releases only", r.stdout)
        out = self.p.hook("pre-tool", as_agent(None, "Bash", command=f"sh {self.script} v0.5.2"))
        self.assertEqual(decision(out), "deny", "the deploy command run directly")
        sh(self.p.root, "git", "tag", "-a", "v0.5.3", "-m", "x")
        self.p.commit(".regista/status.md", "# Status\n", "status")  # only the harness's records since the tag
        r = cli(self.p, "deploy")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(text(self.deployed).strip(), "v0.5.3")

    def test_dropping_tagged_deploys_needs_the_user(self):
        cfg = regista.load_config(self.p.root)
        cfg["deploy"] = {"run": "true"}
        self.assertIn("deploys no longer need a release tag", regista.config_loosened(self.p.root, cfg))


if __name__ == "__main__":
    unittest.main()
