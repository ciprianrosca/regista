"""The suite on cloud shards (/regista:shards). The project's part sits behind the config's "shards" entry: a
`start` command that records a run and writes one session prompt per shard (its last line {"prompts": [...]}),
and a `status` command with the long checks' protocol. `regista.py shards start` refuses a commit no remote
branch has, caps the number of shards, runs `start` and records the run; `shards status` records the verdict.
A gate counts a collected sharded pass as its own only when it opts in ("accept_shards"), for the same code.

Run: python3 -m unittest tests.test_shards -v"""
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_regista import Project, bash, decision, regista, sh  # noqa: E402
from test_release import cli, text  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class Shards(unittest.TestCase):
    """A project with a local bare remote, and a "shards" entry whose start logs its arguments and writes prompt
    files, and whose status reads a file."""

    gates = None
    extra = {}

    def setUp(self):
        self.box = tempfile.mkdtemp(prefix="regista-shards-")
        self.calls = os.path.join(self.box, "calls")
        self.status_file = os.path.join(self.box, "status")
        self.entry = {"start": f"sh {self.box}/start.sh {{sha}} {{short}} {{label}} '{{suite}}' {{n}}",
                      "status": f"echo '{{label}} {{short}}' >> {self.box}/asked; cat {self.status_file}"}
        self.entry.update(self.extra)
        self.starts("""
            echo "$1 $2 $3 [$4] $5" >> {calls}
            i=1; list=""
            while [ $i -le $5 ]; do
              f={box}/prompt-$i.md; echo "run shard $i of $5 on $1" > $f
              list="$list${{list:+,}}\\"$f\\""; i=$((i+1))
            done
            echo "prepared"
            echo "{{\\"prompts\\": [$list]}}"
        """)
        self.p = Project(gates=self.gates or [{"name": "tests", "run": "echo ok"}], shards=self.entry)
        self.bare = os.path.join(self.box, "remote.git")
        sh(self.box, "git", "init", "-q", "--bare", self.bare)
        sh(self.p.root, "git", "remote", "add", "origin", self.bare)
        self.p.commit("src/a.py", "x = 1\n")
        sh(self.p.root, "git", "push", "-q", "origin", "main")

    def tearDown(self):
        self.p.cleanup()
        shutil.rmtree(self.box, ignore_errors=True)

    def starts(self, script):
        body = "\n".join(line[12:] if line.startswith(" " * 12) else line for line in script.strip("\n").splitlines())
        with open(os.path.join(self.box, "start.sh"), "w") as f:
            f.write(body.format(calls=self.calls, box=self.box) + "\n")

    def say(self, words):
        with open(self.status_file, "w") as f:
            f.write(words)

    def call_lines(self):
        return text(self.calls).splitlines() if os.path.exists(self.calls) else []

    def head(self):
        return sh(self.p.root, "git", "rev-parse", "HEAD").strip()

    def runs(self):
        return regista.shards_runs(self.p.root)

    def started(self, *args):
        r = cli(self.p, "shards", "start", *args)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return self.runs()[-1]


class Start(Shards):
    def test_it_parses_the_prompts_records_the_run_and_prints_the_next_steps(self):
        r = cli(self.p, "shards", "start", "HEAD", "--suite", "slow", "--shards", "3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        sha = self.head()
        [line] = self.call_lines()
        got_sha, short, label, suite, n = line.split()
        self.assertEqual((got_sha, short, suite, n), (sha, sha[:7], "[slow]", "3"))
        self.assertRegex(label, r"^shards-%s-\d{8}-\d{6}$" % sha[:7])
        run = self.runs()[-1]
        self.assertEqual((run["label"], run["sha"], run["suite"], run["n"], run["state"]),
                         (label, sha, "slow", 3, "running"))
        self.assertEqual(run["prompts"], [os.path.join(self.box, f"prompt-{k}.md") for k in (1, 2, 3)])
        for k in (1, 2, 3):
            self.assertIn(f"{k}/3: {self.box}/prompt-{k}.md", r.stdout)
        self.assertIn(f"source_revision = {sha}", r.stdout)
        self.assertIn(f"title = \"Demo shards {label} K/3\"", r.stdout)
        self.assertIn(f"regista.py shards status {label}", r.stdout)
        self.assertIn("smallest capable current model", r.stdout)
        self.assertIn("Nothing has started yet", r.stdout)

    def test_the_default_suite_is_empty_and_the_default_count_is_the_cap(self):
        run = self.started()
        [line] = self.call_lines()
        self.assertTrue(line.endswith(" [] 6"), line)
        self.assertEqual((run["suite"], run["n"]), ("", 6))

    def test_a_running_run_on_the_same_commit_and_suite_is_not_prepared_twice(self):
        self.started("--shards", "2")
        r = cli(self.p, "shards", "start", "--shards", "2")
        self.assertEqual(r.returncode, 0)
        self.assertIn("already runs", r.stdout)
        self.assertEqual(len(self.call_lines()), 1)
        self.assertEqual(cli(self.p, "shards", "start", "--shards", "2", "--fresh").returncode, 0)
        self.assertEqual(len(self.call_lines()), 2, "--fresh prepares another")

    def test_fewer_prompts_than_shards_are_fine_more_are_refused(self):
        self.starts(f"""
            f={self.box}/only.md; echo one > $f
            echo "{{{{\\"prompts\\": [\\"$f\\"]}}}}"
        """)
        self.assertEqual(self.started("--shards", "4")["n"], 1)
        self.starts(f"""
            echo a > {self.box}/a.md; echo b > {self.box}/b.md
            echo '{{{{"prompts": ["{self.box}/a.md", "{self.box}/b.md"]}}}}'
        """)
        r = cli(self.p, "shards", "start", "--shards", "1", "--suite", "other")
        self.assertEqual(r.returncode, 1)
        self.assertIn("wrote 2 prompts for 1 shards", r.stdout)
        self.assertEqual(len(self.runs()), 1, "not recorded")

    def test_relative_prompt_paths_are_read_from_the_repository(self):
        self.p.write("out/p1.md", "go\n")
        self.starts("""
            echo '{{"prompts": ["out/p1.md"]}}'
        """)
        self.assertEqual(self.started()["prompts"], ["out/p1.md"])
        self.starts("""
            echo '{{"prompts": ["out/missing.md"]}}'
        """)
        r = cli(self.p, "shards", "start", "--suite", "x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("out/missing.md", r.stdout)

    def test_no_prompts_line_is_a_failure(self):
        self.starts("""
            echo done
        """)
        r = cli(self.p, "shards", "start")
        self.assertEqual(r.returncode, 1)
        self.assertIn('{"prompts": ["path", ...]}', r.stdout)
        self.assertEqual(self.runs(), [])


class Caps(Shards):
    extra = {"max_sessions": 2, "model": "small-model-x"}

    def test_the_number_of_shards_is_capped_by_max_sessions(self):
        r = cli(self.p, "shards", "start", "--shards", "5")
        self.assertEqual(r.returncode, 0, r.stdout)
        [line] = self.call_lines()
        self.assertTrue(line.endswith(" 2"), line)
        self.assertIn("5 shards were asked; this project starts at most 2 sessions", r.stdout)
        self.assertEqual((self.runs()[-1]["n"], self.runs()[-1]["asked"]), (2, 5))
        self.assertIn("model = small-model-x", r.stdout)

    def test_zero_shards_is_refused(self):
        r = cli(self.p, "shards", "start", "--shards", "0")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.call_lines(), [])


class Refusals(Shards):
    def test_an_unpushed_commit_is_refused_and_nothing_runs(self):
        self.p.commit("src/a.py", "x = 2\n")
        r = cli(self.p, "shards", "start")
        self.assertEqual(r.returncode, 1)
        self.assertIn("isn't on any remote branch", r.stdout)
        self.assertIn("never pushes", r.stdout)
        self.assertEqual(self.call_lines(), [])
        self.assertEqual(self.runs(), [])
        # an older commit that is on the remote is fine
        self.assertEqual(cli(self.p, "shards", "start", "HEAD~1").returncode, 0)

    def test_exit_75_means_unavailable_and_starts_nothing(self):
        self.starts("""
            echo "the commit isn't on the server yet"
            exit 75
        """)
        r = cli(self.p, "shards", "start")
        self.assertEqual(r.returncode, 75, r.stdout)
        self.assertIn("can't prepare a sharded run now: the commit isn't on the server yet", r.stdout)
        self.assertEqual(self.runs(), [])

    def test_an_unavailable_line_means_unavailable(self):
        self.starts("""
            echo "unavailable: the session cap is reached"
        """)
        r = cli(self.p, "shards", "start")
        self.assertEqual(r.returncode, 75)
        self.assertIn("the session cap is reached", r.stdout)
        self.assertEqual(self.runs(), [])

    def test_another_failure_is_reported(self):
        self.starts("""
            echo "boom: no such suite"
            exit 3
        """)
        r = cli(self.p, "shards", "start")
        self.assertEqual(r.returncode, 1)
        self.assertIn("exited 3", r.stdout)
        self.assertIn("boom: no such suite", r.stdout)
        self.assertEqual(self.runs(), [])

    def test_no_shards_entry_says_how_to_configure_it(self):
        p = Project(gates=[{"name": "tests", "run": "echo ok"}])
        try:
            r = cli(p, "shards", "start")
            self.assertEqual(r.returncode, 1)
            self.assertIn('There\'s no "shards" entry', r.stdout)
            self.assertIn("{sha} {label} {suite} {n}", r.stdout)
        finally:
            p.cleanup()

    def test_agents_and_workers_may_not_start_a_run(self):
        cmd = f'python3 "{os.path.join(REPO, "scripts", "regista.py")}" shards start'
        out = self.p.hook("pre-tool", {**bash(cmd), "agent_id": "a1", "agent_type": "regista:builder"})
        self.assertEqual(decision(out), "deny")
        self.assertNotEqual(decision(self.p.hook("pre-tool", bash(cmd))), "deny", "the main session may")
        status = f'python3 "{os.path.join(REPO, "scripts", "regista.py")}" shards status x'
        self.assertNotEqual(decision(self.p.hook("pre-tool", {**bash(status), "agent_id": "a1",
                                                               "agent_type": "regista:builder"})), "deny")


class Status(Shards):
    def ask(self, label):
        return cli(self.p, "shards", "status", label)

    def test_running_then_pass_is_recorded_with_its_values(self):
        label = self.started("--shards", "2")["label"]
        self.say("running\n")
        r = self.ask(label)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn("still running", r.stdout)
        self.say('{"status": "pass", "passed": 120, "failed": 0, "wall_seconds": 300}\n')
        r = self.ask(label)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("PASSED", r.stdout)
        self.assertIn("passed=120", r.stdout)
        self.assertIn("No gate accepts a sharded pass of this suite", r.stdout)
        run = self.runs()[-1]
        self.assertEqual((run["state"], run["result"]["passed"]), ("pass", 120))
        self.assertIn(f"{label} {self.head()[:7]}", text(os.path.join(self.box, "asked")))
        # a finished run isn't asked again
        self.say("fail\n")
        self.assertEqual(self.ask(label).returncode, 0)
        self.assertIn("PASSED", cli(self.p, "shards", "list").stdout)

    def test_fail_is_recorded(self):
        label = self.started()["label"]
        self.say("tests/test_x.py::test_y failed\nfail\n")
        r = self.ask(label)
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAILED", r.stdout)
        self.assertEqual(self.runs()[-1]["state"], "fail")

    def test_a_result_line_without_status_is_judged_by_the_exit_code(self):
        label = self.started()["label"]
        self.say('{"passed": 3, "failed": 1}\n')
        self.p.write(".x", "")
        self.entry["status"] = f"cat {self.status_file}; exit 1"
        cfg_path = os.path.join(self.p.root, ".regista", "config.json")
        c = json.loads(text(cfg_path))
        c["shards"] = self.entry
        with open(cfg_path, "w") as f:
            json.dump(c, f)
        self.assertEqual(self.ask(label).returncode, 1)
        self.assertEqual(self.runs()[-1]["state"], "fail")

    def test_unavailable_is_waiting(self):
        label = self.started()["label"]
        self.say("unavailable: the results store is down\n")
        r = self.ask(label)
        self.assertEqual(r.returncode, 75)
        self.assertIn("the results store is down", r.stdout)
        self.assertEqual(self.runs()[-1]["state"], "waiting")

    def test_a_run_past_its_timeout_is_timed_out(self):
        label = self.started()["label"]
        path = regista.shards_path(self.p.root)
        data = json.loads(text(path))
        data["runs"][-1]["started"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat(timespec="seconds")
        with open(path, "w") as f:
            json.dump(data, f)
        self.say("running\n")
        r = self.ask(label)
        self.assertEqual(r.returncode, 1)
        self.assertIn("TIMED OUT", r.stdout)
        self.assertEqual(self.runs()[-1]["state"], "timeout")

    def test_an_unknown_label(self):
        r = self.ask("shards-nope")
        self.assertEqual(r.returncode, 3)
        self.assertIn("There's no sharded run 'shards-nope'", r.stdout)

    def test_session_start_mentions_an_open_run(self):
        label = self.started()["label"]
        out = self.p.hook("session-start", {"source": "startup"})
        ctx = json.dumps(out)
        self.assertIn("Runs on cloud shards not reported yet", ctx)
        self.assertIn(label, ctx)


class AcceptShards(Shards):
    """A gate that would fail if it ran here: only a sharded pass it accepts can make it pass."""

    def setUp(self):
        self.ran = tempfile.mktemp(prefix="regista-ran-")
        self.gates = [{"name": "tests", "run": "echo ok"},
                      {"name": "suite", "run": f"touch {self.ran}; exit 1", "when": ["manual"],
                       "accept_shards": {"suite": "full"}},
                      {"name": "plain", "run": f"touch {self.ran}; exit 1", "when": ["manual"]},
                      {"name": "soak", "kind": "detached", "when": ["manual"], "start": "echo started",
                       "status": "echo running", "accept_shards": {"suite": "full"}}]
        super().setUp()

    def tearDown(self):
        if os.path.exists(self.ran):
            os.remove(self.ran)
        super().tearDown()

    def collected(self, words="pass", suite="full"):
        label = self.started("--suite", suite, "--fresh")["label"]
        self.say(words + "\n")
        cli(self.p, "shards", "status", label)
        return label

    def gate(self, name):
        return cli(self.p, "gate", name)

    def test_off_by_default_a_sharded_pass_counts_for_no_gate(self):
        self.collected()
        r = self.gate("plain")
        self.assertEqual(r.returncode, 1)
        self.assertTrue(os.path.exists(self.ran), "it ran here")
        self.assertNotIn("cloud shards", r.stdout)

    def test_on_it_counts_for_the_same_code_and_is_never_recorded_as_a_local_pass(self):
        label = self.collected()
        r = self.gate("suite")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(os.path.exists(self.ran), "it didn't run here")
        self.assertIn(f"- suite: PASS, run on cloud shards (passed on {self.head()[:7]} as {label}", r.stdout)
        passed = regista.load_json(regista.passed_path(self.p.root), {})
        self.assertFalse(any("suite" in (e.get("gates") or {}) for e in passed.values()))
        r = self.gate("soak")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("- soak: PASS, run on cloud shards", r.stdout)
        # a commit of only the harness's records keeps it; status shows which gates take it
        self.p.commit(".regista/notes.md", "n\n")
        self.assertEqual(self.gate("suite").returncode, 0)
        self.assertIn("Gates that accept it as their pass, for the code it ran on: suite, soak",
                      cli(self.p, "shards", "status", label).stdout)

    def test_not_after_a_relevant_change(self):
        self.collected()
        self.p.commit("src/a.py", "x = 3\n")
        r = self.gate("suite")
        self.assertEqual(r.returncode, 1)
        self.assertTrue(os.path.exists(self.ran))
        self.assertEqual(self.gate("soak").returncode, 1)
        self.p.write("src/a.py", "x = 1\n")  # uncommitted changes count too
        self.assertEqual(self.gate("suite").returncode, 1)

    def test_with_when_changed_only_its_paths_matter(self):
        cfg_path = os.path.join(self.p.root, ".regista", "config.json")
        c = json.loads(text(cfg_path))
        c["gates"][1]["when_changed"] = ["src/**"]
        with open(cfg_path, "w") as f:
            json.dump(c, f)
        sh(self.p.root, "git", "commit", "-qam", "config")
        sh(self.p.root, "git", "push", "-q", "origin", "main")
        self.collected()
        self.p.commit("docs/x.md", "d\n")
        self.assertEqual(self.gate("suite").returncode, 0)
        self.p.commit("src/a.py", "x = 4\n")
        self.assertEqual(self.gate("suite").returncode, 1)

    def test_never_while_running_waiting_failed_or_for_another_suite(self):
        self.started("--suite", "full")
        self.assertEqual(self.gate("suite").returncode, 1, "running")
        for words in ("unavailable: down", "fail"):
            self.collected(words)
            self.assertEqual(self.gate("suite").returncode, 1, words)
        self.collected("pass", suite="smoke")
        self.assertEqual(self.gate("suite").returncode, 1, "another suite")

    def test_a_metrics_gate_never_accepts_one(self):
        cfg = regista.load_config(self.p.root)
        self.collected()
        g = {**cfg["gates"][1], "metrics": {"score": {"min": 1}}}
        self.assertIsNone(regista.shards_cover(self.p.root, cfg, g))
        self.assertIsNotNone(regista.shards_cover(self.p.root, cfg, cfg["gates"][1]))


class Loosening(Shards):
    def test_adding_or_changing_shards_or_accept_shards_needs_the_users_ok(self):
        cfg = regista.load_config(self.p.root)
        self.assertEqual(regista.config_loosened(self.p.root, cfg), [])
        changed = {**cfg, "shards": {**self.entry, "status": "echo pass"}}
        self.assertTrue(any('"shards"' in x for x in regista.config_loosened(self.p.root, changed)))
        self.assertEqual(regista.config_loosened(self.p.root, {**cfg, "shards": None}), [], "removing it isn't")
        opted = {**cfg, "gates": [{**cfg["gates"][0], "accept_shards": {"suite": ""}}]}
        self.assertTrue(any("accept_shards" in x for x in regista.config_loosened(self.p.root, opted)))
        new_gate = {**cfg, "gates": cfg["gates"] + [{"name": "more", "run": "echo", "accept_shards": True}]}
        self.assertTrue(any("'more' now accepts" in x for x in regista.config_loosened(self.p.root, new_gate)))

    def test_adding_shards_to_a_project_without_it_is_a_loosening(self):
        p = Project(gates=[{"name": "tests", "run": "echo ok"}])
        try:
            cfg = regista.load_config(p.root)
            self.assertTrue(regista.config_loosened(p.root, {**cfg, "shards": self.entry}))
        finally:
            p.cleanup()


class Doctor(Shards):
    gates = [{"name": "tests", "run": "echo ok", "accept_shards": {"suite": "full"}}]

    def test_doctor_says_shards_are_configured_and_which_gates_accept_them(self):
        out = cli(self.p, "doctor").stdout
        self.assertIn('the suite can run on cloud shards ("shards":', out)
        self.assertIn("at most 6 sessions a run, 60 minutes to report", out)
        self.assertIn("gates that accept a pass on cloud shards as their own: tests (suite full)", out)

    def test_doctor_without_shards(self):
        p = Project(gates=[{"name": "tests", "run": "echo ok"}])
        try:
            out = cli(p, "doctor").stdout
            self.assertIn('no "shards" entry', out)
        finally:
            p.cleanup()


class SourceUrl(unittest.TestCase):
    def test_remote_addresses_become_https(self):
        for given, want in (("git@example.org:team/app.git", "https://example.org/team/app"),
                            ("ssh://git@example.org/team/app.git", "https://example.org/team/app"),
                            ("https://user@example.org/team/app.git", "https://example.org/team/app"),
                            ("https://example.org/team/app", "https://example.org/team/app"),
                            ("/srv/git/app.git", "")):
            self.assertEqual(regista.https_url(given), want, given)


class TheSkill(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(REPO, "skills", "shards", "SKILL.md"), encoding="utf-8") as f:
            self.text = f.read()

    def test_only_a_person_starts_it(self):
        head = self.text.split("---")[1]
        self.assertIn("name: shards", head)
        self.assertIn("disable-model-invocation: true", head)
        self.assertIn("argument-hint:", head)
        self.assertIn("allow_implicit_invocation: false",
                      text(os.path.join(REPO, "skills", "shards", "agents", "openai.yaml")))

    def test_it_names_the_tools_and_the_cli(self):
        for needle in ("${CLAUDE_PLUGIN_ROOT}/scripts/regista.py", "create_session", "send_later", "shards start",
                       "shards status", 'claude --cloud "/regista:shards', "ONE turn"):
            self.assertIn(needle, self.text)

    def test_it_is_generic(self):
        self.assertIsNone(re.search(r"github\.com/|/home/|~/|\bssh\b", self.text))


if __name__ == "__main__":
    unittest.main()
