"""A long check that can only run from a machine that has something (brand photos kept on one server, the
production host). Klotho, November 2026: from a cloud session, where ssh to the staging server is refused, the
check's start command failed and Regista said "didn't start (exit 255)", the same as a broken command. A start
command that exits 75 or ends with "unavailable: <why>", or a status command that answers "unavailable", now
records the run as waiting for a machine that can run it: open, never a pass, and replaced by a later start."""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_regista import Project, regista  # noqa: E402
from test_release import cli, text  # noqa: E402


class WaitingForAMachine(unittest.TestCase):
    def setUp(self):
        self.box = tempfile.mkdtemp(prefix="regista-box-")
        self.status = os.path.join(self.box, "status")
        self.polled = os.path.join(self.box, "polled")
        self.gate = {"name": "end-to-end", "kind": "detached", "when": ["milestone", "release"],
                     "needs": ["brand-photos"], "start": f"sh {self.box}/start.sh",
                     "status": f"echo x >> {self.polled}; cat {self.status}"}
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}, self.gate])
        self.p.commit("src/a.py", "x = 1\n")

    def tearDown(self):
        self.p.cleanup()
        shutil.rmtree(self.box, ignore_errors=True)

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
        self.starts("echo 'ssh: connect to host staging port 22: Connection refused'\nexit 75\n")
        r = cli(self.p, "gate", "end-to-end", "--start")
        self.assertEqual(r.returncode, 75, r.stdout + r.stderr)
        self.assertIn("'end-to-end' can't run from this machine: ssh: connect to host staging port 22: Connection "
                      "refused. It needs brand-photos. It stays open until it runs from a machine that can (start it "
                      "there with `regista.py gate end-to-end --start`).", r.stdout)
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
        self.starts("echo checking\necho 'unavailable: the brand photos live on the staging server only'\n")
        r = cli(self.p, "gate", "end-to-end", "--start")
        self.assertEqual(r.returncode, 75, r.stdout)
        self.assertIn("can't run from this machine: the brand photos live on the staging server only.", r.stdout)
        self.assertEqual(self.runs()[-1]["reason"], "the brand photos live on the staging server only")
        self.assertEqual(self.result()["status"], "fail")

    def test_any_other_failure_still_reads_as_didnt_start(self):
        self.starts("echo 'no such script'\nexit 2\n")
        r = cli(self.p, "gate", "end-to-end", "--start")
        self.assertEqual(r.returncode, 1)
        self.assertIn("didn't start (its start command exited 2)", r.stdout)
        self.assertEqual(self.runs(), [])

    def test_a_later_start_from_a_machine_that_can_replaces_it(self):
        self.starts("exit 75\n")
        cli(self.p, "gate", "end-to-end", "--start")
        self.starts(f"echo running > {self.status}\n")
        r = cli(self.p, "gate", "end-to-end", "--start")  # no --fresh needed
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("Started the long check 'end-to-end'", r.stdout)
        self.assertEqual([x["state"] for x in self.runs()], ["running"], "the waiting run was replaced")
        self.say("pass")
        self.assertIn("PASSED", cli(self.p, "gate", "--collect").stdout)
        self.assertEqual(self.result()["status"], "pass")

    def test_collect_doesnt_ask_a_waiting_run_or_time_it_out(self):
        self.starts("exit 75\n")
        cli(self.p, "gate", "end-to-end", "--start")
        path = os.path.join(self.p.root, ".regista", "gates", "detached.json")
        data = json.loads(text(path))
        data["runs"][-1]["started"] = (dt.datetime.now() - dt.timedelta(hours=9)).isoformat(timespec="seconds")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.say("pass")
        out = cli(self.p, "gate", "--collect").stdout
        self.assertEqual(self.polls(), 0, "nothing runs, so its status isn't asked")
        self.assertIn("end-to-end on", out)
        self.assertIn("waiting for a machine that can run it", out)
        self.assertNotIn("time limit", out)
        self.assertEqual(self.runs()[-1]["state"], "waiting")
        self.assertEqual(self.result()["status"], "fail", "a status that says pass doesn't make it one")
        ctx = self.p.hook("session-start", {"source": "startup"})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("waiting for a machine that can run it", ctx)

    def test_a_status_that_says_unavailable_makes_a_running_one_wait(self):
        self.starts(f"echo running > {self.status}\n")
        cli(self.p, "gate", "end-to-end", "--start")
        self.say("unavailable: ssh to staging refused from here\n")
        out = cli(self.p, "gate", "--collect").stdout
        self.assertIn("waiting for a machine that can run it", out)
        self.assertIn("ssh to staging refused from here", out)
        self.assertEqual(self.runs()[-1]["state"], "waiting")
        self.assertEqual(self.result()["status"], "fail")
        self.assertIn("ssh to staging refused from here", self.result()["fails"][0])
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


if __name__ == "__main__":
    unittest.main()
