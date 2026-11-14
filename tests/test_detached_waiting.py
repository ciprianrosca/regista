"""A check that only some machines can run (one that needs a GPU, a device lab, a licence server). Without a way
to say so, a start command that couldn't run from this machine read as "didn't start (exit 255)", the same as a
broken command. Now:

- a start command that exits 75 or ends with "unavailable: <why>", or a status command that answers
  "unavailable", records the run as waiting for a machine that can run it: open, never a pass, and replaced by
  a later start;
- a machine may say what it provides (REGISTA_PROVIDES, or the harness folder's local machine.json); when it
  does and that doesn't cover the gate's "needs", the start command isn't run at all and the run waits;
- an ordinary gate with "needs" that the machine says it can't cover isn't run either, and counts as failed;
- a machine that says nothing behaves exactly as before."""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_regista import Project, regista  # noqa: E402
from test_release import cli, text  # noqa: E402

VAR = "REGISTA_PROVIDES"


class NoProvisionsFromOutside(unittest.TestCase):
    """The tests decide what the machine provides: a REGISTA_PROVIDES set where they run doesn't leak in."""

    def setUp(self):
        self.saved = os.environ.pop(VAR, None)

    def tearDown(self):
        if self.saved is not None:
            os.environ[VAR] = self.saved


class WaitingForAMachine(NoProvisionsFromOutside):
    def setUp(self):
        super().setUp()
        self.box = tempfile.mkdtemp(prefix="regista-box-")
        self.status = os.path.join(self.box, "status")
        self.polled = os.path.join(self.box, "polled")
        self.started = os.path.join(self.box, "started")
        self.gate = {"name": "hardware", "kind": "detached", "when": ["milestone", "release"],
                     "needs": ["gpu"], "start": f"echo x >> {self.started}; sh {self.box}/start.sh",
                     "status": f"echo x >> {self.polled}; cat {self.status}"}
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}, self.gate])
        self.p.commit("src/a.py", "x = 1\n")

    def tearDown(self):
        self.p.cleanup()
        shutil.rmtree(self.box, ignore_errors=True)
        super().tearDown()

    def starts(self, script):
        with open(os.path.join(self.box, "start.sh"), "w") as f:
            f.write(script)

    def say(self, words):
        with open(self.status, "w") as f:
            f.write(words)

    def result(self):
        return regista.detached_result(self.p.root, regista.load_config(self.p.root), self.gate)

    def runs(self):
        return regista.detached_runs(self.p.root)

    def polls(self):
        return len(text(self.polled).split()) if os.path.exists(self.polled) else 0

    def test_exit_75_records_a_waiting_run_that_never_passes(self):
        self.starts("echo 'the device lab is offline'\nexit 75\n")
        r = cli(self.p, "gate", "hardware", "--start")
        self.assertEqual(r.returncode, 75, r.stdout + r.stderr)
        self.assertIn("'hardware' can't run from this machine: the device lab is offline. It needs gpu. It stays "
                      "open until it runs from a machine that can (start it there with `regista.py gate hardware "
                      "--start`).", r.stdout)
        self.assertNotIn("didn't start", r.stdout)
        self.assertEqual(self.runs()[-1]["state"], "waiting")
        res = self.result()
        self.assertEqual(res["status"], "fail")
        self.assertEqual(res["elsewhere"], "waiting for a machine that can run it")
        self.assertIn("can't run from this machine", res["fails"][0])
        cfg = regista.load_config(self.p.root)
        self.assertTrue(any("can't run from this machine" in x
                            for x in regista.long_check_reasons(self.p.root, cfg, "milestone")))
        self.assertIn("FAIL, run elsewhere (waiting for a machine that can run it)", regista.format_results([res]))

    def test_an_unavailable_last_line_records_a_waiting_run_with_its_reason(self):
        self.starts("echo checking\necho 'unavailable: no licence server reachable from here'\n")
        r = cli(self.p, "gate", "hardware", "--start")
        self.assertEqual(r.returncode, 75, r.stdout)
        self.assertIn("can't run from this machine: no licence server reachable from here.", r.stdout)
        self.assertEqual(self.runs()[-1]["reason"], "no licence server reachable from here")
        self.assertEqual(self.result()["status"], "fail")

    def test_any_other_failure_still_reads_as_didnt_start(self):
        self.starts("echo 'no such script'\nexit 2\n")
        r = cli(self.p, "gate", "hardware", "--start")
        self.assertEqual(r.returncode, 1)
        self.assertIn("didn't start (its start command exited 2)", r.stdout)
        self.assertEqual(self.runs(), [])

    def test_a_later_start_from_a_machine_that_can_replaces_it(self):
        self.starts("exit 75\n")
        cli(self.p, "gate", "hardware", "--start")
        self.starts(f"echo running > {self.status}\n")
        r = cli(self.p, "gate", "hardware", "--start")  # no --fresh needed
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("Started the long check 'hardware'", r.stdout)
        self.assertEqual([x["state"] for x in self.runs()], ["running"], "the waiting run was replaced")
        self.say("pass")
        self.assertIn("PASSED", cli(self.p, "gate", "--collect").stdout)
        self.assertEqual(self.result()["status"], "pass")

    def test_collect_doesnt_ask_a_waiting_run_or_time_it_out(self):
        self.starts("exit 75\n")
        cli(self.p, "gate", "hardware", "--start")
        path = os.path.join(self.p.root, ".regista", "gates", "detached.json")
        data = json.loads(text(path))
        data["runs"][-1]["started"] = (dt.datetime.now() - dt.timedelta(hours=9)).isoformat(timespec="seconds")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.say("pass")
        out = cli(self.p, "gate", "--collect").stdout
        self.assertEqual(self.polls(), 0, "nothing runs, so its status isn't asked")
        self.assertIn("hardware on", out)
        self.assertIn("waiting for a machine that can run it", out)
        self.assertNotIn("time limit", out)
        self.assertEqual(self.runs()[-1]["state"], "waiting")
        self.assertEqual(self.result()["status"], "fail", "a status that says pass doesn't make it one")
        ctx = self.p.hook("session-start", {"source": "startup"})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("waiting for a machine that can run it", ctx)

    def test_a_status_that_says_unavailable_makes_a_running_one_wait(self):
        self.starts(f"echo running > {self.status}\n")
        cli(self.p, "gate", "hardware", "--start")
        self.say("unavailable: the runner lost its gpu\n")
        out = cli(self.p, "gate", "--collect").stdout
        self.assertIn("waiting for a machine that can run it", out)
        self.assertIn("the runner lost its gpu", out)
        self.assertEqual(self.runs()[-1]["state"], "waiting")
        self.assertEqual(self.result()["status"], "fail")
        self.assertIn("the runner lost its gpu", self.result()["fails"][0])
        polled = self.polls()
        cli(self.p, "gate", "--collect")
        self.assertEqual(self.polls(), polled, "a waiting run isn't asked again")

    def test_the_status_words(self):
        self.assertEqual(regista.detached_verdict(0, "unavailable"), ("waiting", {"reason": "no reason given"}))
        self.assertEqual(regista.detached_verdict(1, "Unavailable: no route"), ("waiting", {"reason": "no route"}))
        self.assertEqual(regista.detached_verdict(0, '{"status": "unavailable", "reason": "offline"}'),
                         ("waiting", {"reason": "offline"}))
        self.assertEqual(regista.detached_verdict(0, "waiting")[0], "running", "a queued run is still running")
        self.assertEqual(regista.detached_verdict(0, "pass")[0], "pass")

    # --- what the machine says it provides ---------------------------------------------------------------------

    def test_a_machine_that_says_it_lacks_a_need_waits_without_running_start(self):
        self.starts(f"echo running > {self.status}\n")
        r = cli(self.p, "gate", "hardware", "--start", env={VAR: "device-lab, licence-server"})
        self.assertEqual(r.returncode, 75, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.started), "the start command wasn't run")
        self.assertIn("'hardware' can't run from this machine: this machine doesn't provide gpu (it provides: "
                      "device-lab, licence-server).", r.stdout)
        self.assertEqual(self.runs()[-1]["state"], "waiting")
        self.assertEqual(self.runs()[-1]["reason"],
                         "this machine doesn't provide gpu (it provides: device-lab, licence-server)")
        self.assertEqual(self.result()["status"], "fail")
        cfg = regista.load_config(self.p.root)
        self.assertTrue(any("doesn't provide gpu" in x
                            for x in regista.long_check_reasons(self.p.root, cfg, "release")))

    def test_an_empty_declaration_provides_nothing(self):
        self.starts(f"echo running > {self.status}\n")
        r = cli(self.p, "gate", "hardware", "--start", env={VAR: ""})
        self.assertEqual(r.returncode, 75, r.stdout)
        self.assertFalse(os.path.exists(self.started))
        self.assertIn("doesn't provide gpu (it provides: nothing)", r.stdout)

    def test_the_local_machine_file_says_it_too_and_is_never_committed(self):
        self.starts(f"echo running > {self.status}\n")
        self.p.write(".regista/machine.json", json.dumps({"provides": ["device-lab"]}))
        r = cli(self.p, "gate", "hardware", "--start")
        self.assertEqual(r.returncode, 75, r.stdout)
        self.assertFalse(os.path.exists(self.started))
        self.assertIn("doesn't provide gpu (it provides: device-lab)", r.stdout)
        ignored = subprocess.run(["git", "check-ignore", "-q", ".regista/machine.json"], cwd=self.p.root)
        self.assertEqual(ignored.returncode, 0, "machine.json is local, in the harness folder's .gitignore")
        # The file and the variable count together; names match whatever their case.
        r = cli(self.p, "gate", "hardware", "--start", env={VAR: "GPU"})
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(os.path.exists(self.started))

    def test_a_machine_that_provides_every_need_runs_the_start_command(self):
        self.starts(f"echo running > {self.status}\n")
        r = cli(self.p, "gate", "hardware", "--start", env={VAR: "device-lab,gpu"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(self.started))
        self.assertIn("Started the long check 'hardware'", r.stdout)
        self.assertEqual(self.runs()[-1]["state"], "running")
        self.assertEqual(self.result()["status"], "fail", "started is not passed")

    def test_a_machine_that_says_nothing_runs_the_start_command_as_before(self):
        self.starts("exit 75\n")
        r = cli(self.p, "gate", "hardware", "--start")
        self.assertTrue(os.path.exists(self.started), "nothing declared: the start command decides")
        self.assertEqual(r.returncode, 75)
        self.assertNotIn("doesn't provide", r.stdout)
        self.assertIsNone(regista.machine_provides(self.p.root))

    def test_a_wrong_claim_only_lets_the_start_command_run_and_say_so(self):
        self.starts("echo 'unavailable: no gpu here after all'\n")
        r = cli(self.p, "gate", "hardware", "--start", env={VAR: "gpu"})
        self.assertEqual(r.returncode, 75, r.stdout)
        self.assertTrue(os.path.exists(self.started))
        self.assertEqual(self.result()["status"], "fail")


class AnOrdinaryGateWithNeeds(NoProvisionsFromOutside):
    def setUp(self):
        super().setUp()
        self.box = tempfile.mkdtemp(prefix="regista-box-")
        self.ran = os.path.join(self.box, "ran")
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"},
                                {"name": "hardware", "run": f"touch {self.ran}", "needs": ["gpu"],
                                 "when": ["manual"]}])
        self.p.commit("src/a.py", "x = 1\n")

    def tearDown(self):
        self.p.cleanup()
        shutil.rmtree(self.box, ignore_errors=True)
        super().tearDown()

    def test_it_isnt_run_and_fails_when_the_machine_lacks_a_need(self):
        r = cli(self.p, "gate", "hardware", env={VAR: "device-lab"})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.ran), "the gate's command wasn't run")
        self.assertIn("- hardware: FAIL, not run (waiting for a machine that can run it)", r.stdout)
        self.assertIn("'hardware' can't run on this machine: this machine doesn't provide gpu (it provides: "
                      "device-lab)", r.stdout)
        last = json.loads(text(os.path.join(self.p.root, ".regista", "gates", "last.json")))
        self.assertEqual([x["status"] for x in last["results"] if x["name"] == "hardware"], ["fail"])
        # Not kept as a pass to reuse: a run on a machine that has a gpu runs it.
        r = cli(self.p, "gate", "hardware", env={VAR: "gpu"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(self.ran))

    def test_it_runs_when_the_machine_provides_it_or_says_nothing(self):
        r = cli(self.p, "gate", "hardware", env={VAR: "gpu"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(self.ran))
        os.remove(self.ran)
        r = cli(self.p, "gate", "hardware", "--fresh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(self.ran))

    def test_a_waiting_result_never_counts_as_ok(self):
        cfg = regista.load_config(self.p.root)
        os.environ[VAR] = "device-lab"
        try:
            results = regista.run_gates(self.p.root, cfg, regista.select_gates(cfg, names=["hardware"]))
        finally:
            del os.environ[VAR]
        self.assertEqual([r["status"] for r in results], ["fail"])
        self.assertEqual(results[0]["waiting"], regista.WAITING)
        self.assertFalse(os.path.exists(self.ran))


if __name__ == "__main__":
    unittest.main()
