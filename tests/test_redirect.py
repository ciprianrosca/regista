"""Sending a check elsewhere. A gate this machine can't run (its "needs" aren't covered, or a long check's own start
says it's unavailable from here) is sent, best effort, through the config's "elsewhere" entry: a start and a status
command with the long checks' protocol. A run it starts is collected like a long check, and its pass counts as the
gate's own only for the code it ran on. After a deploy, a gate this machine can't run is no failure: the release
stays live, marked unchecked, and a run sent elsewhere that later fails on the live release rolls it back."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_detached_waiting import VAR, NoProvisionsFromOutside  # noqa: E402
from test_regista import Project, regista, sh  # noqa: E402
from test_release import TODAY, cli, set_config, text  # noqa: E402

LACKS = {VAR: "device-lab"}  # a machine without the gpu the "hardware" gate needs
HAS = {VAR: "gpu"}


class Box(NoProvisionsFromOutside):
    """A scratch folder with a redirect whose start logs its placeholders and a status that reads a file."""

    def setUp(self):
        super().setUp()
        self.box = tempfile.mkdtemp(prefix="regista-box-")
        self.ran = os.path.join(self.box, "ran")
        self.sent = os.path.join(self.box, "sent")
        self.status = os.path.join(self.box, "status")
        self.redirect = {"start": f"echo '{{name}} {{sha}} {{short}} {{label}} {{needs}}' >> {self.sent}; "
                                  f"sh {self.box}/send.sh",
                         "status": f"cat {self.status}"}
        self.sends("echo queued\n")

    def tearDown(self):
        shutil.rmtree(self.box, ignore_errors=True)
        super().tearDown()

    def sends(self, script):
        with open(os.path.join(self.box, "send.sh"), "w") as f:
            f.write(script)

    def say(self, words):
        with open(self.status, "w") as f:
            f.write(words)

    def sent_lines(self):
        return text(self.sent).splitlines() if os.path.exists(self.sent) else []

    def runs(self):
        return regista.detached_runs(self.p.root)


class AnOrdinaryGateSentElsewhere(Box):
    def setUp(self):
        super().setUp()
        self.gate = {"name": "hardware", "run": f"touch {self.ran}", "needs": ["gpu"], "when": ["manual"]}
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}, self.gate], elsewhere=self.redirect)
        self.p.commit("src/a.py", "x = 1\n")

    def tearDown(self):
        self.p.cleanup()
        super().tearDown()

    def head(self):
        return sh(self.p.root, "git", "rev-parse", "HEAD").strip()

    def test_it_is_sent_with_its_placeholders_and_its_pass_counts_for_that_code_only(self):
        r = cli(self.p, "gate", "hardware", env=LACKS)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.ran), "its command didn't run here")
        self.assertIn("- hardware: FAIL, not run (running elsewhere via the redirect)", r.stdout)
        sha = self.head()
        [line] = self.sent_lines()
        name, got_sha, short, label, needs = line.split()
        self.assertEqual((name, got_sha, short, needs), ("hardware", sha, sha[:7], "gpu"))
        self.assertTrue(label.startswith(f"hardware-{sha[:7]}-"))
        run = self.runs()[-1]
        self.assertEqual((run["via"], run["state"], run["sha"], run["run"]), ("elsewhere", "running", sha, self.gate["run"]))
        # asked again while it runs: not sent twice, still no pass
        r = cli(self.p, "gate", "hardware", env=LACKS)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.sent_lines()), 1)
        self.assertIn("running elsewhere via the redirect", r.stdout)
        # collected through the redirect's status command: a pass counts as the gate's own, for this code
        self.say("pass")
        out = cli(self.p, "gate", "--collect").stdout
        self.assertIn("hardware on", out)
        self.assertIn("PASSED", out)
        r = cli(self.p, "gate", "hardware", env=LACKS)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("hardware: PASS, run elsewhere (passed on", r.stdout)
        self.assertFalse(os.path.exists(self.ran))
        # new code: that pass doesn't hold, and the new commit is sent instead
        self.p.commit("src/b.py", "y = 1\n")
        r = cli(self.p, "gate", "hardware", env=LACKS)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertNotIn("PASS", r.stdout)
        self.assertEqual(self.sent_lines()[-1].split()[1], self.head())
        # uncommitted changes: the redirect runs commits, so nothing is sent
        self.p.write("src/c.py", "z = 1\n")
        r = cli(self.p, "gate", "hardware", env=LACKS)
        self.assertEqual(r.returncode, 1)
        self.assertIn("once the change is committed", r.stdout)
        self.assertEqual(len(self.sent_lines()), 2)

    def test_a_redirect_that_finds_no_machine_leaves_it_waiting(self):
        self.sends("echo 'no gpu runner is free'\nexit 75\n")
        r = cli(self.p, "gate", "hardware", env=LACKS)
        self.assertEqual(r.returncode, 1)
        self.assertIn("- hardware: FAIL, not run (waiting for a machine that can run it)", r.stdout)
        self.assertIn("The redirect found no machine that can run it either: no gpu runner is free.", r.stdout)
        self.assertEqual(self.runs(), [])
        self.sends("echo 'unavailable: the queue is closed'\n")
        r = cli(self.p, "gate", "hardware", env=LACKS)
        self.assertIn("no machine that can run it either: the queue is closed", r.stdout)
        self.assertEqual(self.runs(), [])

    def test_a_redirect_that_breaks_is_reported_and_never_a_pass(self):
        self.sends("echo 'bad credentials'\nexit 2\n")
        r = cli(self.p, "gate", "hardware", env=LACKS)
        self.assertEqual(r.returncode, 1)
        self.assertIn("It couldn't be sent elsewhere: the redirect's start command exited 2: bad credentials.", r.stdout)
        self.assertEqual(self.runs(), [])

    def test_without_a_redirect_it_waits_as_before(self):
        set_config(self.p, elsewhere=None)
        r = cli(self.p, "gate", "hardware", env=LACKS)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not run (waiting for a machine that can run it)", r.stdout)
        self.assertEqual(self.sent_lines(), [])

    def test_a_machine_that_can_run_it_runs_it_here(self):
        r = cli(self.p, "gate", "hardware", env=HAS)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(os.path.exists(self.ran))
        self.assertEqual(self.sent_lines(), [])

    def test_a_failed_run_elsewhere_on_this_code_is_a_real_failure(self):
        cli(self.p, "gate", "hardware", env=LACKS)
        self.say("fail")
        self.assertIn("FAILED", cli(self.p, "gate", "--collect").stdout)
        cfg = regista.load_config(self.p.root)
        os.environ[VAR] = LACKS[VAR]
        try:
            [res] = regista.run_gates(self.p.root, cfg, regista.select_gates(cfg, names=["hardware"]))
        finally:
            del os.environ[VAR]
        self.assertEqual(res["status"], "fail")
        self.assertNotIn("waiting", res, "it ran, and failed: a verdict on the code")
        self.assertEqual(len(self.sent_lines()), 1, "not sent again without --fresh")
        r = cli(self.p, "gate", "hardware", "--fresh", env=LACKS)
        self.assertEqual(len(self.sent_lines()), 2, "--fresh sends it again")
        self.assertIn("running elsewhere via the redirect", r.stdout)

    def test_adding_or_changing_the_redirect_is_a_loosening(self):
        cfg = regista.load_config(self.p.root)
        self.assertEqual(regista.config_loosened(self.p.root, cfg), [])
        changed = {**cfg, "elsewhere": {**self.redirect, "status": "echo pass"}}
        self.assertTrue(any('"elsewhere"' in x for x in regista.config_loosened(self.p.root, changed)))
        self.assertEqual(regista.config_loosened(self.p.root, {**cfg, "elsewhere": None}), [], "removing it isn't")

    def test_doctor_names_the_redirect_and_the_gates_it_would_take(self):
        r = cli(self.p, "doctor", env=LACKS)
        self.assertIn("checks this machine can't run are sent elsewhere via the redirect", r.stdout)
        self.assertIn("check 'hardware' can't run on this machine: this machine doesn't provide gpu (it provides: "
                      "device-lab); it would be sent elsewhere via the redirect", r.stdout)


class WhenChangedElsewhere(Box):
    def setUp(self):
        super().setUp()
        gate = {"name": "hardware", "run": f"touch {self.ran}", "needs": ["gpu"], "when": ["manual"],
                "when_changed": ["src/**"]}
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}, gate], elsewhere=self.redirect)
        self.p.commit("src/a.py", "x = 1\n")

    def tearDown(self):
        self.p.cleanup()
        super().tearDown()

    def test_a_pass_on_an_older_commit_holds_only_until_its_paths_change(self):
        cli(self.p, "gate", "hardware", env=LACKS)
        self.say("pass")
        cli(self.p, "gate", "--collect")
        self.p.commit("docs/notes.md", "notes\n")
        r = cli(self.p, "gate", "hardware", env=LACKS)
        self.assertEqual(r.returncode, 0, "nothing it checks changed since: the pass holds\n" + r.stdout)
        self.p.commit("src/a.py", "x = 2\n")
        r = cli(self.p, "gate", "hardware", env=LACKS)
        self.assertEqual(r.returncode, 1, "a relevant change since: the older pass isn't accepted\n" + r.stdout)
        self.assertIn("running elsewhere via the redirect", r.stdout)


class ALongCheckSentElsewhere(Box):
    def setUp(self):
        super().setUp()
        self.own = os.path.join(self.box, "own-start")
        self.gate = {"name": "hardware", "kind": "detached", "when": ["milestone", "release"], "needs": ["gpu"],
                     "start": f"echo x >> {self.own}; echo 'no gpu here'; exit 75",
                     "status": "echo 'unavailable: not from here'"}
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}, self.gate], elsewhere=self.redirect)
        self.p.commit("src/a.py", "x = 1\n")

    def tearDown(self):
        self.p.cleanup()
        super().tearDown()

    def result(self):
        return regista.detached_result(self.p.root, regista.load_config(self.p.root), self.gate)

    def test_its_own_start_saying_unavailable_tries_the_redirect(self):
        r = cli(self.p, "gate", "hardware", "--start")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(self.own), "its own start ran first")
        self.assertIn("'hardware' can't run from this machine (no gpu here), so it was sent elsewhere via the "
                      "redirect", r.stdout)
        run = self.runs()[-1]
        self.assertEqual((run["via"], run["state"], run["start"]), ("elsewhere", "running", self.gate["start"]))
        self.assertEqual(self.result()["status"], "fail", "sent is not passed")
        self.say("pass")  # asked through the redirect's status, not the gate's own (which says unavailable)
        self.assertIn("PASSED", cli(self.p, "gate", "--collect").stdout)
        self.assertEqual(self.result()["status"], "pass")

    def test_a_machine_without_its_needs_goes_straight_to_the_redirect(self):
        r = cli(self.p, "gate", "hardware", "--start", env=LACKS)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(os.path.exists(self.own), "its own start wasn't run")
        self.assertEqual(self.sent_lines()[-1].split()[-1], "gpu")

    def test_with_no_machine_anywhere_it_waits(self):
        self.sends("exit 75\n")
        r = cli(self.p, "gate", "hardware", "--start")
        self.assertEqual(r.returncode, 75, r.stdout)
        self.assertIn("The redirect found no machine that can run it either", r.stdout)
        self.assertEqual(self.runs()[-1]["state"], "waiting")
        self.assertEqual(self.result()["status"], "fail")


class AfterADeploy(Box):
    """An after-deploy gate this machine can't run leaves the release live, unchecked; sent elsewhere, it carries
    the rollback a long check started by the deploy carries."""

    def setUp(self):
        super().setUp()
        self.deployed = os.path.join(self.box, "deployed")
        script = os.path.join(self.box, "deploy.sh")
        with open(script, "w") as f:
            f.write(f'echo "$1" > {self.deployed}\n')
        self.gate = {"name": "hardware", "run": f"echo x >> {self.ran}", "needs": ["gpu"], "when": ["release"],
                     "after_deploy": True}
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}, self.gate], push="block",
                         release={"version_files": ["pyproject.toml"], "heading": "## {version} — {date}"},
                         deploy={"run": f"sh {script} {{ref}}", "ref": "tag"})
        self.p.write("pyproject.toml", '[project]\nname = "demo_app"\nversion = "0.5.2"\n')
        self.p.commit("CHANGELOG.md", "# Changelog\n\n## 0.5.2 — 2026-10-01\n\nEarlier.\n", "0.5.2")
        sh(self.p.root, "git", "tag", "-a", "v0.5.2", "-m", "0.5.2")

    def tearDown(self):
        self.p.cleanup()
        super().tearDown()

    def ship(self, version, env):
        self.p.commit("src/v.py", f"v = '{version}'\n", f"work for {version}")
        self.p.write("CHANGELOG.md", f"# Changelog\n\n## {version} — {TODAY}\n\nChanges.\n\n## 0.5.2 — 2026-10-01\n\n"
                                     "Earlier.\n")
        r = cli(self.p, "release", "--no-push", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return cli(self.p, "deploy", env=env)

    def live(self):
        return text(self.deployed).strip()

    def test_without_a_redirect_the_release_stays_live_and_unchecked(self):
        r = self.ship("0.5.3", HAS)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("hardware: PASS", r.stdout)
        r = self.ship("0.5.4", LACKS)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.live(), "v0.5.4", "not rolled back")
        self.assertNotIn("live again", r.stdout)
        self.assertEqual(len(text(self.ran).split()), 1, "its command didn't run here")
        self.assertIn("'hardware' can't run on this machine: this machine doesn't provide gpu (it provides: "
                      "device-lab). It's waiting for a machine that can run it, not a failure, so the release stays "
                      "live.", r.stdout)
        self.assertIn("Nothing has checked this release yet, and nothing rolls it back until this check runs.", r.stdout)
        status = cli(self.p, "status", env=LACKS).stdout
        self.assertIn("Not checked yet after the deploy: hardware (waiting for a machine that can run it)", status)
        ctx = self.p.hook("session-start", {"source": "startup"})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("hardware on", ctx)
        self.assertIn("waiting for a machine that can run it", ctx)
        # run where it can: the marker goes
        self.assertEqual(cli(self.p, "gate", "hardware", "--fresh", env=HAS).returncode, 0)
        self.assertNotIn("Not checked yet", cli(self.p, "status", env=HAS).stdout)

    def test_a_run_sent_elsewhere_that_fails_on_the_live_release_rolls_it_back(self):
        set_config(self.p, elsewhere=self.redirect)
        self.assertEqual(self.ship("0.5.3", HAS).returncode, 0)
        r = self.ship("0.5.4", LACKS)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.live(), "v0.5.4")
        tagged = sh(self.p.root, "git", "rev-parse", "v0.5.4^{commit}").strip()
        self.assertEqual(self.sent_lines()[-1].split()[1], tagged, "sent on the deployed commit")
        self.assertIn("so it was sent elsewhere via the redirect", r.stdout)
        self.assertIn("If it fails, v0.5.3 goes back live.", r.stdout)
        self.assertIn("Not checked yet after the deploy: hardware (running elsewhere via the redirect)",
                      cli(self.p, "status", env=LACKS).stdout)
        run = self.runs()[-1]
        self.assertEqual((run["deployed"], run["rollback_to"]), ("v0.5.4", "v0.5.3"))
        self.say("running")
        cli(self.p, "gate", "--collect")
        self.assertEqual(self.live(), "v0.5.4")
        self.say("fail")
        out = cli(self.p, "gate", "--collect").stdout
        self.assertIn("the check hardware failed on v0.5.4, so v0.5.3 is live again", out)
        self.assertEqual(self.live(), "v0.5.3")
        self.assertNotIn("live again", cli(self.p, "gate", "--collect").stdout)

    def test_a_run_sent_elsewhere_that_passes_leaves_it_live(self):
        set_config(self.p, elsewhere=self.redirect)
        self.assertEqual(self.ship("0.5.3", HAS).returncode, 0)
        self.assertEqual(self.ship("0.5.4", LACKS).returncode, 0)
        self.say("pass")
        self.assertIn("PASSED", cli(self.p, "gate", "--collect").stdout)
        self.assertEqual(self.live(), "v0.5.4")
        self.assertNotIn("Not checked yet", cli(self.p, "status", env=LACKS).stdout)


class RequirementEvidence(Box):
    def setUp(self):
        super().setUp()
        from test_regista import E2E_GATE, SPEC
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"},
                                {**E2E_GATE, "needs": ["gpu"], "when": ["manual"]}], elsewhere=self.redirect)
        self.p.write(".regista/specs/stats.json", json.dumps(SPEC))
        self.p.write("tests/test_stats.py", "# covers: STATS-1\n")
        self.p.write("e2e/cli_test.sh", "# covers: STATS-2\n")
        sh(self.p.root, "git", "add", "-A")
        sh(self.p.root, "git", "commit", "-q", "-m", "specs")

    def tearDown(self):
        self.p.cleanup()
        super().tearDown()

    def test_a_collected_pass_of_an_end_to_end_gate_on_head_records_its_requirements(self):
        cli(self.p, "gate", "e2e", env=LACKS)
        state = regista.load_json(regista.requirements_path(self.p.root), {})
        self.assertFalse(state.get("STATS-2", {}).get("passes"), "sent is not passed")
        self.say("pass")
        cli(self.p, "gate", "--collect")
        state = regista.load_json(regista.requirements_path(self.p.root), {})
        self.assertTrue(state["STATS-2"]["passes"])
        self.assertEqual(state["STATS-2"]["evidence"], ["e2e"])


if __name__ == "__main__":
    unittest.main()
