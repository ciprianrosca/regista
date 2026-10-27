"""Old projects keep working (MIG-1), the old typed command forms still count (MIG-7), and a warning when the
old plugin is still enabled next to the new one (MIG-8).

An old project is one set up by Daedalus 0.8: its harness folder is .daedalus/ (config, plan, specs, gates,
state) and it has no .regista/ folder. The tests build one in a temporary git repository: today's `init` runs in
a scratch folder, and its result is copied under .daedalus/ with every .regista path in it rewritten to
.daedalus. Then scripts/regista.py is driven as a subprocess, the way Claude Code and Codex drive it.

Every run uses a temporary project as its working folder and a temporary home folder: the repository's own
.daedalus/ folder holds the build's records, and this machine's home folder may enable the old plugin.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_migration -v"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# test_regista points HOME and CODEX_HOME at a temporary folder when it is imported.
from test_regista import (BIG, E2E_GATE, FAKE, FLEET_PLAN, PY, SPEC, TOOL, TRY_SPEC, Project, as_agent,  # noqa: E402
                          bash, clean_env, decision, jload, queued, regista, sh)

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

OLD = ".daedalus"
NEW = ".regista"


# --- helpers --------------------------------------------------------------------------------------------------

def run_tool(cwd, *args, env=None, stdin=None):
    """Run scripts/regista.py from `cwd` (always a temporary folder) with a clean environment."""
    return subprocess.run([PY, TOOL, *args], cwd=cwd, input=stdin, capture_output=True, text=True, encoding="utf-8",
                          env=env if env is not None else clean_env())


def run_hook(cwd, event, payload=None, host=None, env=None):
    """Drive one hook with JSON on stdin, from `cwd`, and return its parsed answer (None when it says nothing)."""
    data = {"cwd": cwd, **(payload or {})}
    r = subprocess.run([PY, TOOL, "hook", event, *(["--host", host] if host else [])], input=json.dumps(data),
                       cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       env=env if env is not None else clean_env())
    if not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        raise AssertionError(f"hook {event} answered with something that isn't JSON:\n{r.stdout[-800:]}\n"
                             f"{r.stderr[-800:]}")


def context(out):
    return ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""


def reason(out):
    return ((out or {}).get("hookSpecificOutput") or {}).get("permissionDecisionReason") or ""


def without_tool_path(text):
    """The text with the harness's own location taken out: while it is being built the repository sits under a
    .daedalus folder, so the tool path printed in answers says .daedalus even in a new project."""
    return text.replace(REPO, "<harness>")


def old_folder_lines(text):
    """The lines that tell the user the project still uses the old folder and /regista:start can move it."""
    return [ln for ln in without_tool_path(text).splitlines() if OLD in ln and "/regista:start" in ln]


def folders_named(top, name):
    """Every folder called `name` under `top`, not looking inside .git."""
    found = []
    for dirpath, dirnames, _ in os.walk(top):
        if ".git" in dirnames:
            dirnames.remove(".git")
        found += [os.path.join(dirpath, d) for d in dirnames if d == name]
    return found


def load(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))


def copy_as_old(src, dest):
    """Copy a .regista folder to `dest`, rewriting every .regista path inside its files to .daedalus."""
    for dirpath, _, files in os.walk(src):
        target = os.path.join(dest, os.path.relpath(dirpath, src))
        os.makedirs(target, exist_ok=True)
        for name in files:
            with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                text = f.read()
            with open(os.path.join(target, name), "w", encoding="utf-8") as f:
                f.write(text.replace(NEW, OLD))


class OldProject:
    """A project set up by Daedalus 0.8: .daedalus/config.json with .daedalus/ paths, and no .regista/ folder."""

    def __init__(self, gates=None, protected=None, **cfg):
        self.root = tempfile.mkdtemp(prefix="regista-old-")
        sh(self.root, "git", "init", "-q", "-b", "main")
        sh(self.root, "git", "config", "user.email", "t@example.com")
        sh(self.root, "git", "config", "user.name", "T")
        made = tempfile.mkdtemp(prefix="regista-init-")
        try:
            sh(made, "git", "init", "-q", "-b", "main")
            r = run_tool(made, "init", "--name", "Demo", env=clean_env(CLAUDE_PROJECT_DIR=made))
            if r.returncode != 0 or not os.path.isfile(os.path.join(made, NEW, "config.json")):
                raise AssertionError("couldn't make the old project's folder with init:\n" + r.stdout + r.stderr)
            copy_as_old(os.path.join(made, NEW), os.path.join(self.root, OLD))
        finally:
            shutil.rmtree(made, ignore_errors=True)
        path = self.path("config.json")
        c = load(path)
        assert c["plan"] == ".daedalus/plan.md" and c["state_dir"] == ".daedalus/state", c
        c.update(gates=gates or [], protected=protected or [], **cfg)
        save(path, c)
        self.write("README.md", "demo\n")
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "init")

    def path(self, *parts):
        return os.path.join(self.root, OLD, *parts)

    def write(self, rel, text):
        save(os.path.join(self.root, *rel.split("/")), text)

    def commit(self, rel, text, msg="work"):
        self.write(rel, text)
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", msg)

    def hook(self, event, payload=None, cwd=None, host=None, env=None):
        return run_hook(cwd or self.root, event, payload, host=host, env=env)

    def cli(self, *args, cwd=None, env=None):
        return run_tool(cwd or self.root, *args, env=env)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


STAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def comparable(record):
    """A requirement record without the parts that change from one run to the next (times)."""
    return {k: v for k, v in record.items()
            if not (isinstance(v, str) and STAMP.search(v))
            and not (isinstance(v, (int, float)) and not isinstance(v, bool) and v > 1e9)}


# --- MIG-1: an old project keeps working on .daedalus/ ----------------------------------------------------------

class OldProjectWorks(unittest.TestCase):
    """A project with .daedalus/config.json and no .regista/ works as it did in 0.8, on .daedalus/."""

    def make(self, **kw):
        p = OldProject(**kw)
        self.addCleanup(p.cleanup)
        self.assertEqual(folders_named(p.root, NEW), [])
        return p

    def assertNoNewFolder(self, p):
        found = folders_named(p.root, NEW)
        self.assertEqual(found, [], "a .regista folder appeared in the old project: " + ", ".join(found))
        self.assertTrue(os.path.isfile(p.path("config.json")), "the old project's .daedalus/config.json is gone")

    def test_session_start_says_once_that_the_project_uses_the_old_folder(self):  # covers: MIG-1
        p = self.make()
        p.write(".daedalus/state/2026-01-03-x.md", "# h\n## Resume point\nWire the exporter.\n## Blocked on\n- nothing\n")
        out = p.hook("session-start", {"source": "startup"})
        self.assertIsNotNone(out, "session start said nothing in an old project")
        ctx = context(out)
        self.assertIn("runs on the Regista harness", ctx)
        self.assertIn("Wire the exporter.", ctx)  # the handoff is read from .daedalus/state
        lines = old_folder_lines(ctx)
        self.assertEqual(len(lines), 1, "expected one line naming .daedalus and /regista:start:\n" + ctx)
        self.assertIn(NEW, lines[0], lines[0])  # where /regista:start would move it
        self.assertNotIn("/daedalus:", ctx)  # commands are still named /regista:<skill>
        self.assertNoNewFolder(p)  # nothing moves before the user says yes
        compacted = context(p.hook("session-start", {"source": "compact"}))
        self.assertIn("compacted", compacted)
        self.assertLessEqual(len(old_folder_lines(compacted)), 1, compacted)
        self.assertNoNewFolder(p)

    def test_a_new_project_gets_no_old_folder_line(self):  # covers: MIG-1
        p = Project()
        self.addCleanup(p.cleanup)
        ctx = context(run_hook(p.root, "session-start", {"source": "startup"}))
        self.assertIn("runs on the Regista harness", ctx)
        self.assertEqual(old_folder_lines(ctx), [], ctx)
        self.assertNotIn(OLD, without_tool_path(ctx))

    def test_a_project_with_both_folders_is_a_new_project(self):  # covers: MIG-1
        p = Project(gates=[{"name": "tests", "run": "echo ok", "when": ["commit"]}])
        self.addCleanup(p.cleanup)
        old_cfg = load(os.path.join(p.root, NEW, "config.json"))
        old_cfg = json.loads(json.dumps(old_cfg).replace(NEW, OLD))
        old_cfg["gates"] = [{"name": "old-tests", "run": "echo old gate ran; exit 3", "when": ["commit"]}]
        p.write(".daedalus/config.json", json.dumps(old_cfg))
        p.write(".daedalus/plan.md", "# Old plan\n")
        sh(p.root, "git", "add", "-A")
        sh(p.root, "git", "commit", "-q", "-m", "an old folder next to the new one")
        ctx = context(run_hook(p.root, "session-start", {"source": "startup"}))
        self.assertEqual(old_folder_lines(ctx), [], ctx)
        r = run_tool(p.root, "gate")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)  # the passing gate from .regista/config.json
        self.assertNotIn("old gate ran", r.stdout)
        self.assertTrue(os.path.isfile(os.path.join(p.root, NEW, "gates", "last.json")))
        self.assertFalse(os.path.exists(os.path.join(p.root, OLD, "gates")))
        self.assertIsNone(run_hook(p.root, "pre-tool", as_agent(None, "Bash", command="git commit -m x")))

    def test_status_reads_the_old_folder(self):  # covers: MIG-1
        p = self.make()
        status = p.path("status.md")
        with open(status, encoding="utf-8") as f:
            text = f.read()
        text = text.replace("<the single next concrete step>", "Wire the exporter next.")
        if "Wire the exporter next." not in text:
            text += "\n## Next\nWire the exporter next.\n"
        p.commit(".daedalus/status.md", text, "docs: status")
        r = p.cli("status")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("# Regista: Demo", r.stdout)
        self.assertIn("Wire the exporter next.", r.stdout)
        self.assertIn(".daedalus/status.md", r.stdout)
        self.assertNoNewFolder(p)

    def test_requirements_reads_specs_from_the_old_folder(self):  # covers: MIG-1
        p = self.make(gates=[{"name": "tests", "run": "echo ok"}, E2E_GATE])
        p.write(".daedalus/specs/stats.json", json.dumps(SPEC))
        r = p.cli("requirements")
        self.assertIn("STATS-1", r.stdout, r.stdout + r.stderr)
        self.assertIn("STATS-2", r.stdout)
        self.assertNoNewFolder(p)

    def test_gate_writes_results_and_records_under_the_old_folder(self):  # covers: MIG-1
        p = self.make(gates=[{"name": "tests", "run": "echo ok", "when": ["commit"]}])
        p.write(".daedalus/specs/stats.json", json.dumps({**SPEC, "requirements": [SPEC["requirements"][0]]}))
        p.write("tests/test_stats.py", "# covers: STATS-1\n")
        sh(p.root, "git", "add", "-A")
        sh(p.root, "git", "commit", "-q", "-m", "tests")
        r = p.cli("gate")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isfile(p.path("gates", "last.json")), "the gate run should write .daedalus/gates/last.json")
        self.assertTrue(load(p.path("gates", "requirements.json"), {})["STATS-1"]["passes"])
        self.assertNoNewFolder(p)

    def test_commit_flow_checks_requirements_and_records_them_in_the_old_folder(self):  # covers: MIG-1
        p = self.make(gates=[{"name": "tests", "run": "echo ok"}, E2E_GATE])
        p.write(".daedalus/specs/stats.json", json.dumps(SPEC))
        commit = as_agent(None, "Bash", command="git add -A && git commit -m feat")
        out = p.hook("pre-tool", commit)
        self.assertEqual(decision(out), "deny", out)
        self.assertIn("STATS-1", reason(out))
        self.assertIn("STATS-2", reason(out))
        p.write("tests/test_stats.py", "# covers: STATS-1\n")
        p.write("e2e/cli_test.sh", "# covers: STATS-2\n")
        self.assertIsNone(p.hook("pre-tool", commit))
        state = load(p.path("gates", "requirements.json"), {})
        self.assertTrue(state["STATS-1"]["passes"])
        self.assertTrue(state["STATS-2"]["passes"])
        r = p.cli("milestone", "M1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNoNewFolder(p)

    def test_failing_gate_blocks_the_commit(self):  # covers: MIG-1
        p = self.make(gates=[{"name": "tests", "run": "echo boom; exit 3", "when": ["commit"]}])
        out = p.hook("pre-tool", bash("git add -A && git commit -m wip"))
        self.assertEqual(decision(out), "deny")
        self.assertIn("tests: FAIL", reason(out))
        self.assertIn("boom", reason(out))
        self.assertNoNewFolder(p)

    def test_pre_tool_guards_work_as_in_a_new_project(self):  # covers: MIG-1
        p = self.make(protected=["accuracy/truth/**"])
        out = p.hook("pre-tool", as_agent(None, "Write", file_path=os.path.join(p.root, "accuracy", "truth", "a.json"),
                                          content="x"))
        self.assertEqual(decision(out), "deny")
        self.assertTrue(reason(out).startswith("Regista: "), out)
        # orchestrated mode (init's template): the main session doesn't edit source, a builder does
        src = os.path.join(p.root, "src", "a.py")
        out = p.hook("pre-tool", as_agent(None, "Edit", file_path=src))
        self.assertEqual(decision(out), "deny", out)
        self.assertTrue(reason(out).startswith("Regista (orchestrated mode): "), out)
        self.assertIsNone(p.hook("pre-tool", as_agent("builder", "Edit", file_path=src)))
        out = p.hook("pre-tool", bash("git push origin main"))
        self.assertEqual(decision(out), "ask")
        self.assertTrue(reason(out).startswith("Regista: "), out)
        self.assertEqual(decision(p.hook("pre-tool", bash("git commit --no-verify -m x"))), "deny")
        # the harness's own files in .daedalus/ are guarded like those in .regista/
        cfgfile = p.path("config.json")
        self.assertEqual(decision(p.hook("pre-tool", as_agent(None, "Edit", file_path=cfgfile))), "ask")
        self.assertEqual(decision(p.hook("pre-tool", as_agent("builder", "Edit", file_path=cfgfile))), "deny")
        for role in (None, "builder"):
            self.assertEqual(decision(p.hook("pre-tool", as_agent(role, "Write", file_path=p.path(
                "gates", "requirements.json")))), "deny", role)
        self.assertEqual(decision(p.hook("pre-tool", as_agent(None, "Write", file_path=p.path(
            "gates", "baseline.json")))), "deny")
        self.assertEqual(decision(p.hook("pre-tool", bash("echo {} > .daedalus/gates/baseline.json"))), "deny")
        self.assertNoNewFolder(p)

    def test_review_record_reads_and_writes_the_old_folder(self):  # covers: MIG-1
        p = self.make(gates=[{"name": "tests", "run": "echo ok", "when": ["commit"]}],
                      roles={"source": ["src/**"], "tests": ["tests/**"]}, review={"required_for": ["src/**"]},
                      requirements={"enforce_coverage": False, "milestone_needs_e2e": False})
        p.write("src/a.py", BIG)
        commit = as_agent(None, "Bash", command="git add -A && git commit -m feat")
        out = p.hook("pre-tool", commit)
        self.assertEqual(decision(out), "deny", out)
        self.assertIn("no review", reason(out))
        p.write(".daedalus/reviews/draft.json", json.dumps(
            {"verdict": "approve", "summary": "s", "checked": ["spec", "edge cases"], "findings": []}))
        r = p.cli("review-record")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNone(decision(p.hook("pre-tool", commit)))
        self.assertNoNewFolder(p)

    def test_stop_reads_handoffs_and_run_state_from_the_old_folder(self):  # covers: MIG-1
        p = self.make()
        p.commit("src/a.py", "x = 1", "feat: a")
        out = p.hook("stop", {"stop_hook_active": False})
        self.assertEqual((out or {}).get("decision"), "block", out)
        self.assertIn("feat: a", out["reason"])
        self.assertIn("/regista:save", out["reason"])
        time.sleep(1.1)
        p.write(".daedalus/state/2026-01-02-save.md", "# h\n## Resume point\nnext\n")
        self.assertIsNone(p.hook("stop", {}))  # the handoff in .daedalus/state counts
        r = p.cli("run", "start")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(load(p.path("run.json"), {}).get("active"), "run start should write .daedalus/run.json")
        out = p.hook("stop", {})
        self.assertEqual((out or {}).get("decision"), "block", out)
        self.assertIn("build-all run is active", out["reason"])
        self.assertNoNewFolder(p)

    def test_works_from_a_subfolder(self):  # covers: MIG-1
        p = self.make(protected=[".env"])
        sub = os.path.join(p.root, "src", "deep")
        os.makedirs(sub)
        r = p.cli("status", cwd=sub)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("# Regista: Demo", r.stdout)
        ctx = context(p.hook("session-start", {"source": "startup"}, cwd=sub))
        self.assertEqual(len(old_folder_lines(ctx)), 1, ctx)
        out = p.hook("pre-tool", as_agent(None, "Write", file_path=os.path.join(p.root, ".env"), content="x"), cwd=sub)
        self.assertEqual(decision(out), "deny")
        self.assertEqual(folders_named(sub, OLD), [])  # no second harness folder in the subfolder
        self.assertNoNewFolder(p)

    def test_codex_hooks_work_on_the_old_folder(self):  # covers: MIG-1
        p = self.make(gates=[{"name": "tests", "run": "echo ok"}], protected=[".env"])
        ctx = context(p.hook("session-start", {"session_id": "c1", "source": "startup"}, host="codex"))
        self.assertIn("runs on the Regista harness", ctx)
        refused = p.hook("pre-tool", {"session_id": "c1", "tool_name": "apply_patch", "tool_input": {
            "command": "*** Begin Patch\n*** Add File: .env\n+x\n*** End Patch"}}, host="codex")
        self.assertEqual(decision(refused), "deny")
        self.assertIn(".env is protected", reason(refused))
        self.assertNoNewFolder(p)


class OldProjectWorkers(unittest.TestCase):
    """Workers in an old project keep the old branch prefix, worktree folder and gate lock, so workers started
    by 0.8 can still be merged and removed."""

    def setUp(self):
        self.p = OldProject(gates=[{"name": "tests", "run": "test ! -f broken.txt", "when": ["commit"]}],
                            review={"required_for": []},
                            requirements={"enforce_coverage": False, "milestone_needs_e2e": False})
        self.p.commit(".daedalus/plan.md", FLEET_PLAN, "plan")
        self.fake = tempfile.mkdtemp(prefix="regista-fake-")
        for tool in ("claude", "tmux"):
            path = os.path.join(self.fake, tool)
            with open(path, "w", encoding="utf-8") as f:
                f.write(FAKE.replace("PYTHON", sys.executable))
            os.chmod(path, 0o755)
        self.env = clean_env(REGISTA_CLAUDE=os.path.join(self.fake, "claude"),
                             REGISTA_TMUX=os.path.join(self.fake, "tmux"), FAKE_DIR=self.fake,
                             REGISTA_LAUNCH_WAIT="3", CLAUDE_CODE_SESSION_ID="overseer-1")

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.p.root, capture_output=True)
        self.p.cleanup()
        shutil.rmtree(self.fake, ignore_errors=True)

    def wt(self, name="m2"):
        return self.p.path("worktrees", name)

    def fleet(self, *args):
        return self.p.cli("fleet", *args, env=self.env)

    def calls(self, tool):
        with open(os.path.join(self.fake, "calls.jsonl"), encoding="utf-8") as f:
            return [c for c in map(json.loads, f) if c["tool"] == tool]

    def spawn(self, target="M2"):
        self.p.hook("pre-tool", {**bash(f'python3 "{TOOL}" fleet spawn {target}'), "session_id": "overseer-1",
                                 "permission_mode": "acceptEdits"}, env=self.env)
        return self.fleet("spawn", target)

    def test_a_worker_keeps_the_old_branch_folder_and_gate_lock(self):  # covers: MIG-1
        r = self.spawn()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.wt(), "README.md")),
                        "the worktree should be under .daedalus/worktrees/m2")
        self.assertEqual(sh(self.wt(), "git", "rev-parse", "--abbrev-ref", "HEAD").strip(), "daedalus/m2")
        info = jload(os.path.join(self.wt(), OLD, "worker.json"))
        self.assertEqual(info["milestone"], "M2")
        self.assertEqual(jload(self.p.path("fleet", "workers.json"))["m2"]["state"], "running")
        self.assertIn("/regista:build M2", self.calls("tmux")[0]["args"][-1])
        self.assertEqual(sh(self.p.root, "git", "status", "--porcelain").strip(), "")  # the worktrees are ignored
        self.assertTrue(regista.gate_lock_path(self.wt()).endswith("daedalus-gates.lock"))
        self.assertEqual(regista.gate_lock_path(self.wt()), regista.gate_lock_path(self.p.root))
        self.assertEqual(folders_named(self.p.root, NEW), [])

    def test_a_worker_can_be_merged_and_removed(self):  # covers: MIG-1
        self.assertEqual(self.spawn().returncode, 0)
        save(os.path.join(self.wt(), "src", "pages.py"), "pages = 1\n")
        sh(self.wt(), "git", "add", "-A")
        sh(self.wt(), "git", "commit", "-q", "-m", "feat: pages")
        r = self.fleet("merge", "m2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.p.root, "src", "pages.py")))
        r = self.fleet("remove", "m2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.wt()))
        self.assertEqual(sh(self.p.root, "git", "branch", "--list", "daedalus/m2").strip(), "")
        self.assertEqual(folders_named(self.p.root, NEW), [])


ROLES = ("builder", "test-writer", "reviewer", "researcher")


def patch(*headers):
    body = "\n".join(f"*** {h}\n+x" for h in headers)
    return {"tool_name": "apply_patch", "tool_input": {"command": f"*** Begin Patch\n{body}\n*** End Patch"}}


def codex_agents(project, prefix):
    """The project's Codex agent files, .codex/agents/<prefix><role>.toml, committed."""
    for role in ROLES:
        project.write(f".codex/agents/{prefix}{role}.toml", f'name = "{prefix}{role}"\n')
    sh(project.root, "git", "add", "-A")
    sh(project.root, "git", "commit", "-q", "-m", "codex agents")


class OldProjectCodexAgents(unittest.TestCase):
    """A project set up by 0.8 for Codex has .codex/agents/daedalus-<role>.toml: there the daedalus-<role> agent
    types get the same rights as regista-<role> in a new project, and Codex answers name them."""

    def setUp(self):
        self.p = OldProject(gates=[{"name": "tests", "run": "echo ok"}], protected=[".env"],
                            roles={"source": ["src/**"], "tests": ["tests/**"]})
        self.addCleanup(self.p.cleanup)
        codex_agents(self.p, "daedalus-")

    def hook(self, event, payload, project=None):
        return run_hook((project or self.p).root, event, {"session_id": "c1", **payload}, host="codex")

    def as_codex_agent(self, agent_type, payload):
        return {**payload, "agent_id": "t1", "agent_type": agent_type} if agent_type else payload

    def check(self, agent_type, payload, project=None):
        return decision(self.hook("pre-tool", self.as_codex_agent(agent_type, payload), project))

    def test_old_codex_agent_types_get_their_roles(self):  # covers: MIG-1
        review = bash(f'python "{TOOL}" review-record')
        # the builder writes source, not tests, and doesn't commit
        self.assertIsNone(self.check("daedalus-builder", patch("Add File: src/x.py")))
        self.assertEqual(self.check("daedalus-builder", patch("Add File: tests/test_x.py")), "deny")
        self.assertEqual(self.check("daedalus-builder", bash("git add -A && git commit -m x")), "deny")
        # the test writer writes tests, not source
        self.assertIsNone(self.check("daedalus-test-writer", patch("Add File: tests/test_x.py")))
        self.assertEqual(self.check("daedalus-test-writer", patch("Add File: src/x.py")), "deny")
        # the reviewer records its review, and only the reviewer does
        self.assertIsNone(self.check("daedalus-reviewer", review))
        self.assertIsNone(self.check("daedalus-reviewer", patch("Add File: .daedalus/reviews/draft.json")))
        self.assertEqual(self.check("daedalus-reviewer", patch("Add File: src/x.py")), "deny")
        for agent_type in (None, "daedalus-builder", "daedalus-researcher"):
            self.assertEqual(self.check(agent_type, review), "deny", agent_type)
        # the researcher writes research notes, not source
        self.assertIsNone(self.check("daedalus-researcher", patch("Add File: .daedalus/research/q1.md")))
        self.assertEqual(self.check("daedalus-researcher", patch("Add File: src/x.py")), "deny")
        # the orchestrator still doesn't edit source
        self.assertEqual(self.check(None, patch("Add File: src/x.py")), "deny")
        self.assertEqual(folders_named(self.p.root, NEW), [])

    def test_old_codex_agent_types_match_the_new_ones_in_a_new_project(self):  # covers: MIG-1
        new = Project(gates=[{"name": "tests", "run": "echo ok"}], protected=[".env"],
                      roles={"source": ["src/**"], "tests": ["tests/**"]})
        self.addCleanup(new.cleanup)
        codex_agents(new, "regista-")
        review = bash(f'python "{TOOL}" review-record')
        cases = [(role, payload) for role in ROLES for payload in (
            patch("Add File: src/x.py"), patch("Add File: tests/test_x.py"), review, bash("git commit -m x"))]
        for role, payload in cases:
            with self.subTest(role=role, payload=payload["tool_input"]):
                self.assertEqual(self.check(f"daedalus-{role}", payload), self.check(f"regista-{role}", payload, new))

    def test_codex_session_start_names_the_old_agents(self):  # covers: MIG-1
        ctx = without_tool_path(context(self.hook("session-start", {"source": "startup"})))
        self.assertIn("daedalus-builder", ctx)
        self.assertNotIn("regista-builder", ctx)

    def test_doctor_finds_the_old_codex_agents(self):  # covers: MIG-1
        fake = tempfile.mkdtemp(prefix="regista-codex-")
        self.addCleanup(shutil.rmtree, fake, True)
        save(os.path.join(fake, "bin", "codex"), "#!/bin/sh\necho codex-cli 0.50.0\n")
        os.chmod(os.path.join(fake, "bin", "codex"), 0o755)
        home = os.path.join(fake, "home")
        save(os.path.join(home, ".codex", "config.toml"),
             '[features]\nhooks = true\n\n[plugins."regista@regista"]\nenabled = true\n')
        env = clean_env(HOME=home, CODEX_HOME=os.path.join(home, ".codex"))
        env["PATH"] = os.path.join(fake, "bin") + os.pathsep + env.get("PATH", "")
        r = self.p.cli("doctor", env=env)
        out = without_tool_path(r.stdout + r.stderr)
        self.assertIn("Codex agents: builder, test writer, reviewer, researcher", out)
        self.assertNotIn("Codex agents missing", out)
        self.assertNotIn("regista-builder", out)

    def test_a_new_project_still_uses_only_the_new_agent_types(self):  # covers: MIG-1
        new = Project(gates=[{"name": "tests", "run": "echo ok"}], roles={"source": ["src/**"], "tests": ["tests/**"]})
        self.addCleanup(new.cleanup)
        codex_agents(new, "regista-")
        self.assertIsNone(self.check("regista-builder", patch("Add File: src/x.py"), new))
        self.assertEqual(self.check("daedalus-builder", patch("Add File: src/x.py"), new), "deny")
        self.assertEqual(self.check("daedalus-reviewer", bash(f'python "{TOOL}" review-record'), new), "deny")
        ctx = without_tool_path(context(self.hook("session-start", {"source": "startup"}, new)))
        self.assertIn("regista-builder", ctx)
        self.assertNotIn("daedalus-builder", ctx)


class StartSkill(unittest.TestCase):
    def test_start_skill_offers_the_move_after_a_yes(self):  # covers: MIG-1
        with open(os.path.join(REPO, "skills", "start", "SKILL.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("migrate --check", text)
        self.assertRegex(text, r"migrate(?! --check)", "the skill should also name the plain `migrate` run")
        self.assertIn("old folder", text)
        self.assertNotIn("daedalus", text.lower())


# --- MIG-7: /daedalus:verify and /daedalus:change are recorded like the /regista: forms ---------------------------

class OldCommandForms(unittest.TestCase):
    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}])
        self.p.write(".regista/specs/signup.json", json.dumps(TRY_SPEC))
        self.t = os.path.join(tempfile.mkdtemp(prefix="regista-tr-"), "s1.jsonl")
        open(self.t, "w").close()

    def tearDown(self):
        self.p.cleanup()
        shutil.rmtree(os.path.dirname(self.t), ignore_errors=True)

    def reqfile(self):
        return os.path.join(self.p.root, NEW, "gates", "requirements.json")

    def state(self):
        return load(self.reqfile(), {})

    def forget(self):
        if os.path.exists(self.reqfile()):
            os.remove(self.reqfile())

    def hook(self, event, payload, host=None):
        return run_hook(self.p.root, event, payload, host=host)

    def typed(self, slug, command, args, prompt=None):
        return self.hook("user-prompt-expansion", {"command_name": f"{slug}:{command}", "command_args": args,
                                                   "prompt": prompt or f"/{slug}:{command} {args}"})

    def tool(self):
        return self.hook("pre-tool", {**as_agent(None, "Bash", command="ls"), "transcript_path": self.t})

    def test_typed_daedalus_verify_is_recorded_like_regista_verify(self):  # covers: MIG-7
        results = {}
        for slug in ("regista", "daedalus"):
            self.forget()
            ok = context(self.typed(slug, "verify", "UI-1 ok fits on my Pixel"))
            bad = context(self.typed(slug, "verify", "ui-3 fail the button is purple"))
            self.assertIn("recorded UI-1 as verified", ok, slug)
            self.assertIn("NOT working", bad, slug)
            results[slug] = {k: comparable(v) for k, v in self.state().items()}
        self.assertTrue(results["daedalus"]["UI-1"]["passes"])
        self.assertEqual(results["daedalus"]["UI-1"]["evidence"], ["fits on my Pixel"])
        self.assertFalse(results["daedalus"]["UI-3"]["passes"])
        self.assertEqual(results["daedalus"], results["regista"])

    def test_typed_daedalus_verify_in_any_letter_case(self):  # covers: MIG-7
        out = self.typed("daedalus", "verify", "UI-1 ok", prompt="/Daedalus:VERIFY UI-1 ok")
        self.assertIn("recorded UI-1 as verified", context(out))
        self.assertTrue(self.state()["UI-1"]["passes"])

    def test_both_forms_in_one_message_are_both_recorded(self):  # covers: MIG-7
        out = self.typed("regista", "verify", "UI-1 ok", prompt="/regista:verify UI-1 ok\n/daedalus:verify UI-3 ok")
        self.assertIn("UI-3", context(out))
        self.assertTrue(self.state().get("UI-1", {}).get("passes"), self.state())
        self.assertTrue(self.state().get("UI-3", {}).get("passes"), self.state())

    def test_typed_daedalus_verify_in_codex(self):  # covers: MIG-7
        records = {}
        for form in ("$regista:verify", "$daedalus:verify"):
            self.forget()
            note = context(self.hook("user-prompt", {"session_id": "c1", "prompt": f"{form} UI-1 ok fits"},
                                     host="codex"))
            self.assertIn("recorded UI-1 as verified", note, form)
            records[form] = comparable(self.state()["UI-1"])
        self.assertEqual(records["$daedalus:verify"]["by"], "user (typed in Codex)")
        self.assertEqual(records["$daedalus:verify"], records["$regista:verify"])
        note = context(self.hook("user-prompt", {"session_id": "c1", "prompt": "$DAEDALUS:Verify UI-3 ok"}, host="codex"))
        self.assertIn("recorded UI-3 as verified", note)
        # in Claude Code the dollar form records nothing, as with $regista:verify
        self.forget()
        self.hook("user-prompt", {"prompt": "$daedalus:verify UI-3 ok"})
        self.assertNotIn("UI-3", self.state())

    def test_queued_daedalus_verify_is_recorded_by_the_pre_tool_hook(self):  # covers: MIG-7
        queued(self.t, "/daedalus:verify UI-1 ok fits my phone")
        queued(self.t, "/daedalus:verify UI-3 ok", human=False, uid="u2")  # not a person: ignored
        out = self.tool()
        self.assertIn("sent while you were working", context(out))
        self.assertTrue(self.state()["UI-1"]["passes"])
        self.assertEqual(self.state()["UI-1"]["by"], "user (sent while the session was busy)")
        self.assertEqual(self.state()["UI-1"]["evidence"], ["fits my phone"])
        self.assertNotIn("UI-3", self.state())
        self.assertIsNone(self.tool())  # once only

    def test_queued_daedalus_verify_is_recorded_like_the_regista_form(self):  # covers: MIG-7
        records = {}
        for n, slug in enumerate(("regista", "daedalus")):
            self.forget()
            queued(self.t, f"/{slug}:verify UI-1 ok fits my phone", uid=f"q{n}")
            self.tool()
            records[slug] = comparable(self.state().get("UI-1", {}))
        self.assertTrue(records["daedalus"].get("passes"), records)
        self.assertEqual(records["daedalus"], records["regista"])

    def test_queued_daedalus_verify_is_recorded_by_the_stop_hook(self):  # covers: MIG-7
        queued(self.t, "/daedalus:verify UI-3 fail the logo is cut off", uid="u3")
        out = self.hook("stop", {"transcript_path": self.t})
        self.assertEqual((out or {}).get("decision"), "block", out)
        self.assertIn("NOT working", out["reason"])
        self.assertFalse(self.state()["UI-3"]["passes"])

    def test_queued_daedalus_verify_in_any_letter_case(self):  # covers: MIG-7
        queued(self.t, "/DAEDALUS:Verify UI-1 ok", uid="m1")
        self.tool()
        self.assertTrue(self.state().get("UI-1", {}).get("passes"), self.state())

    def test_other_prefixes_record_nothing(self):  # covers: MIG-7
        queued(self.t, "/notdaedalus:verify UI-1 ok", uid="n1")
        queued(self.t, "/daedalus-old:verify UI-3 ok", uid="n2")
        self.tool()
        self.assertEqual(self.state(), {})
        self.assertIsNone(self.typed("daedalusx", "verify", "UI-1 ok"))
        self.assertEqual(self.state(), {})

    def test_typed_and_queued_daedalus_changes_are_recorded(self):  # covers: MIG-7
        out = self.typed("daedalus", "change", "add club crests, build it last")
        self.assertIn("recorded change 1", context(out))
        queued(self.t, "/daedalus:change the bottom bar gets five slots")
        queued(self.t, "/Daedalus:CHANGE add a price page", uid="u2")
        self.tool()
        start = context(self.hook("session-start", {}))
        self.assertIn("club crests", start)
        self.assertIn("five slots", start)
        self.assertIn("price page", start)
        listed = run_tool(self.p.root, "change").stdout
        self.assertIn("five slots", listed)
        self.assertIn("price page", listed)

    def test_changes_are_recorded_like_the_regista_form(self):  # covers: MIG-7
        texts = {}
        for slug in ("regista", "daedalus"):
            p = Project(gates=[{"name": "tests", "run": "echo ok"}])
            self.addCleanup(p.cleanup)
            ctx = context(run_hook(p.root, "user-prompt-expansion", {
                "command_name": f"{slug}:change", "command_args": "add club crests",
                "prompt": f"/{slug}:change add club crests"}))
            codex = run_hook(p.root, "user-prompt", {"session_id": "c1", "prompt": f"${slug}:change dark mode too"},
                             host="codex")
            listed = run_tool(p.root, "change").stdout
            texts[slug] = (ctx, "club crests" in listed, "dark mode" in listed, codex is None)
        self.assertIn("recorded change 1", texts["daedalus"][0])
        self.assertTrue(texts["daedalus"][1])
        self.assertEqual(texts["daedalus"][1:], texts["regista"][1:])

    def test_old_project_records_daedalus_verify_in_its_own_folder(self):  # covers: MIG-7, MIG-1
        p = OldProject(gates=[{"name": "tests", "run": "echo ok"}])
        self.addCleanup(p.cleanup)
        p.write(".daedalus/specs/signup.json", json.dumps(TRY_SPEC))
        out = p.hook("user-prompt-expansion", {"command_name": "daedalus:verify", "command_args": "UI-1 ok",
                                               "prompt": "/daedalus:verify UI-1 ok"})
        self.assertIn("recorded UI-1 as verified", context(out))
        out = p.hook("user-prompt-expansion", {"command_name": "regista:verify", "command_args": "UI-3 ok",
                                               "prompt": "/regista:verify UI-3 ok"})
        self.assertIn("recorded UI-3 as verified", context(out))
        state = load(p.path("gates", "requirements.json"), {})
        self.assertTrue(state["UI-1"]["passes"] and state["UI-3"]["passes"])
        self.assertEqual(folders_named(p.root, NEW), [])


# --- MIG-8: a warning when the old plugin is still enabled ---------------------------------------------------------

def warning_lines(text):
    return [ln for ln in without_tool_path(text).splitlines() if "daedalus" in ln.lower() and "uninstall" in ln.lower()]


def strings(value):
    """Every string inside a hook answer (context, system message, reasons), however deep."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    return []


class OldPluginStillEnabled(unittest.TestCase):
    """In a new project, doctor and session start warn when the old plugin is enabled next to Regista."""

    def setUp(self):
        self.p = Project(gates=[{"name": "tests", "run": "echo ok"}])
        self.addCleanup(self.p.cleanup)
        self.home = tempfile.mkdtemp(prefix="regista-home-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.env = clean_env(HOME=self.home, CODEX_HOME=os.path.join(self.home, ".codex"))

    def claude_settings(self, where, plugins):
        save(os.path.join(where, ".claude", "settings.json"), {"enabledPlugins": plugins})

    def codex_config(self, text, home=None):
        save(os.path.join(home or os.path.join(self.home, ".codex"), "config.toml"), text)

    def answers(self, root=None, host=None):
        """The warning lines in session start's answer (per text in it) and in doctor's output."""
        root = root or self.p.root
        payload = {"source": "startup", **({"session_id": "c1"} if host else {})}
        out = run_hook(root, "session-start", payload, host=host, env=self.env)
        self.assertIsNotNone(out, "session start said nothing")
        start = [warning_lines(s) for s in strings(out)]
        r = run_tool(root, "doctor", env=self.env)
        return start, warning_lines(r.stdout + r.stderr), json.dumps(out) + "\n" + r.stdout + r.stderr

    def assertWarns(self, host=None, command="claude plugin uninstall daedalus@daedalus"):
        start, doctor, raw = self.answers(host=host)
        found = [ln for lines in start for ln in lines]
        self.assertTrue(found, "session start didn't warn that the old plugin is still enabled:\n" + raw)
        self.assertTrue(all(len(lines) <= 1 for lines in start), "the warning should be one line:\n" + raw)
        self.assertEqual(len(doctor), 1, "doctor should warn in one line:\n" + raw)
        for line in found + doctor:
            self.assertIn("Regista", line)
            self.assertIn("Daedalus", line)
            if command:
                self.assertIn(command, line)

    def assertNoWarning(self, root=None, host=None):
        start, doctor, raw = self.answers(root=root, host=host)
        self.assertEqual([ln for lines in start for ln in lines], [], raw)
        self.assertEqual(doctor, [], raw)

    def test_enabled_in_the_home_settings(self):  # covers: MIG-8
        self.claude_settings(self.home, {"daedalus@daedalus": True, "regista@regista": True})
        self.assertWarns()

    def test_enabled_in_the_project_settings(self):  # covers: MIG-8
        self.claude_settings(self.p.root, {"daedalus@daedalus": True})
        self.assertWarns()

    def test_enabled_in_the_project_local_settings(self):  # covers: MIG-8
        save(os.path.join(self.p.root, ".claude", "settings.local.json"), {"enabledPlugins": {"daedalus@daedalus": True}})
        self.assertWarns()

    def test_enabled_from_another_marketplace(self):  # covers: MIG-8
        self.claude_settings(self.home, {"daedalus@my-plugins": True})
        self.assertWarns(command=None)

    def test_enabled_in_codex(self):  # covers: MIG-8
        self.codex_config('[features]\nhooks = true\n\n[plugins."daedalus@daedalus"]\nenabled = true\n')
        self.assertWarns(host="codex", command=None)

    def test_a_codex_table_without_enabled_counts_as_enabled(self):  # covers: MIG-8
        self.codex_config('[plugins."daedalus@daedalus"]\n')
        self.assertWarns(host="codex", command=None)

    def test_codex_home_defaults_to_the_home_folder(self):  # covers: MIG-8
        del self.env["CODEX_HOME"]
        self.codex_config('[plugins."daedalus@daedalus"]\nenabled = true\n', home=os.path.join(self.home, ".codex"))
        self.assertWarns(host="codex", command=None)

    def test_no_warning_when_switched_off(self):  # covers: MIG-8
        self.claude_settings(self.home, {"daedalus@daedalus": False, "regista@regista": True})
        self.claude_settings(self.p.root, {"daedalus@daedalus": False})
        save(os.path.join(self.p.root, ".claude", "settings.local.json"), {"enabledPlugins": {"daedalus@daedalus": False}})
        self.codex_config('[plugins."daedalus@daedalus"]\nenabled = false\n')
        self.assertNoWarning()
        self.assertNoWarning(host="codex")

    def local_settings(self, plugins):
        save(os.path.join(self.p.root, ".claude", "settings.local.json"), {"enabledPlugins": plugins})

    def test_project_false_overrides_user_true(self):  # covers: MIG-8
        self.claude_settings(self.home, {"daedalus@daedalus": True, "regista@regista": True})
        self.claude_settings(self.p.root, {"daedalus@daedalus": False})
        self.assertNoWarning()

    def test_project_local_false_overrides_user_true(self):  # covers: MIG-8
        self.claude_settings(self.home, {"daedalus@daedalus": True, "regista@regista": True})
        self.local_settings({"daedalus@daedalus": False})
        self.assertNoWarning()

    def test_project_local_false_overrides_project_true(self):  # covers: MIG-8
        self.claude_settings(self.p.root, {"daedalus@daedalus": True})
        self.local_settings({"daedalus@daedalus": False})
        self.assertNoWarning()
        self.claude_settings(self.home, {"daedalus@daedalus": False, "regista@regista": True})
        self.assertNoWarning()

    def test_project_local_true_overrides_both_false(self):  # covers: MIG-8
        self.claude_settings(self.home, {"daedalus@daedalus": False, "regista@regista": True})
        self.claude_settings(self.p.root, {"daedalus@daedalus": False})
        self.local_settings({"daedalus@daedalus": True})
        self.assertWarns()

    def test_project_local_true_overrides_project_false_and_user_true(self):  # covers: MIG-8
        self.claude_settings(self.home, {"daedalus@daedalus": True, "regista@regista": True})
        self.claude_settings(self.p.root, {"daedalus@daedalus": False})
        self.local_settings({"daedalus@daedalus": True})
        self.assertWarns()

    def test_precedence_is_per_plugin_name(self):  # covers: MIG-8
        # The project switches off only daedalus@daedalus; daedalus@my-plugins stays on from the user's settings.
        self.claude_settings(self.home, {"daedalus@daedalus": True, "daedalus@my-plugins": True,
                                         "regista@regista": True})
        self.claude_settings(self.p.root, {"daedalus@daedalus": False})
        self.assertWarns(command=None)

    def test_no_warning_when_nothing_enables_it(self):  # covers: MIG-8
        self.assertNoWarning()
        self.claude_settings(self.home, {"regista@regista": True, "daedalus-tools@x": True})
        self.codex_config('[plugins."regista@regista"]\nenabled = true\n')
        self.assertNoWarning()
        self.assertNoWarning(host="codex")

    def test_a_list_before_enabled_false_doesnt_warn(self):  # covers: MIG-8
        self.codex_config('[plugins."daedalus@daedalus"]\nmcp = ["a"]\nenabled = false\n')
        self.assertNoWarning(host="codex")
        self.codex_config('[plugins."daedalus@daedalus"]\nmcp = [\n  "a",\n  "b",\n]\nenabled = false\n')
        self.assertNoWarning(host="codex")

    def test_a_list_before_enabled_true_warns(self):  # covers: MIG-8
        self.codex_config('[plugins."daedalus@daedalus"]\nmcp = ["a"]\nenabled = true\n')
        self.assertWarns(host="codex", command=None)

    def test_no_warning_in_an_old_project(self):  # covers: MIG-8, MIG-1
        old = OldProject(gates=[{"name": "tests", "run": "echo ok"}])
        self.addCleanup(old.cleanup)
        self.claude_settings(self.home, {"daedalus@daedalus": True, "regista@regista": True})
        self.claude_settings(old.root, {"daedalus@daedalus": True})
        self.codex_config('[plugins."daedalus@daedalus"]\nenabled = true\n')
        self.assertNoWarning(root=old.root)
        ctx = context(run_hook(old.root, "session-start", {"source": "startup"}, env=self.env))
        self.assertEqual(len(old_folder_lines(ctx)), 1, ctx)


# --- MIG-2 to MIG-6: migrate and migrate --check ---------------------------------------------------------------------

MOVE_MESSAGE = "chore: move the harness folder to .regista"


def migrate(cwd, *args):
    """Run `regista.py migrate [args]` from `cwd` (always a temporary project) with a clean environment."""
    return subprocess.run([PY, TOOL, "migrate", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          env=clean_env(), timeout=180)


def said(r):
    return without_tool_path(r.stdout + r.stderr)


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8").stdout


def common_dir(root):
    return os.path.normpath(os.path.join(root, git(root, "rev-parse", "--git-common-dir").strip()))


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def snapshot(root):
    """Everything migrate could change: the files and folders on disk (with contents, ignored files included), the
    index, git's view of the tree, the commits and branches, the worktrees and the exclude file."""
    files, dirs = {}, []
    for dirpath, dirnames, names in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        dirs += [os.path.relpath(os.path.join(dirpath, d), root) for d in dirnames]
        for name in names:
            path = os.path.join(dirpath, name)
            files[os.path.relpath(path, root)] = read_bytes(path)
    exclude = os.path.join(common_dir(root), "info", "exclude")
    return {"files": files, "dirs": sorted(dirs), "status": git(root, "status", "--porcelain", "--ignored"),
            "index": git(root, "ls-files", "-s"), "head": git(root, "rev-parse", "HEAD"),
            "refs": git(root, "for-each-ref"), "worktrees": git(root, "worktree", "list", "--porcelain"),
            "exclude": read_bytes(exclude) if os.path.exists(exclude) else None}


def moved(value):
    """A config value as migrate should leave it: strings that start with .daedalus/ start with .regista/."""
    if isinstance(value, str):
        return NEW + "/" + value[len(OLD) + 1:] if value.startswith(OLD + "/") else value
    if isinstance(value, list):
        return [moved(v) for v in value]
    if isinstance(value, dict):
        return {k: moved(v) for k, v in value.items()}
    return value


LIVE_KEYS = ("plan", "status_file", "board_file", "knowledge_file")

HISTORY = {
    "state/2026-01-01-handoff.md": "# Handoff\n## Resume point\nWire the exporter.\nSee .daedalus/specs/stats.json.\n",
    "research/q1.md": "# Which chart library\nNotes kept in .daedalus/research/; plan in .daedalus/plan.md.\n",
    "reviews/2026-01-01-builder.json": json.dumps({"verdict": "approve", "summary": "checked .daedalus/specs/stats.json",
                                                   "findings": []}),
    "gates/logs/2026-01-01-tests.log": "ran the tests for .daedalus/specs/stats.json: ok\n",
    "specs/stats.json": json.dumps({**SPEC, "requirements": [SPEC["requirements"][0]],
                                    "notes": "research in .daedalus/research/q1.md"}),
}

OLD_AGENTS_MD = ("# Project rules\nBe kind to the build.\n\n<!-- daedalus:start -->\n## Daedalus harness\n"
                 "Read .daedalus/plan.md first.\n<!-- daedalus:end -->\n\n## After the block\nKeep this line.\n")

OLD_EXCLUDE_TAIL = "# my own\n*.swp\n\n# Daedalus workers\n.daedalus/worktrees/\n"

LOCKER = ("import fcntl, sys\nf = open(sys.argv[1], 'a')\nfcntl.flock(f, fcntl.LOCK_EX)\n"
          "print('locked', flush=True)\nsys.stdin.read()\n")


def rich_old_project():
    """An old project with every kind of file migrate touches, committed, plus ignored run state and the fleet's
    block in .git/info/exclude. Returns the project and what was written before the move."""
    p = OldProject(gates=[{"name": "tests", "run": "echo ok", "when": ["commit"]}],
                   no_gates_for=["docs/**", ".daedalus/research/**"],
                   extra={"paths": [".daedalus/specs/a.json", "src/.daedalus/x", ".daedalusx/y",
                                    {"deep": ".daedalus/research", "keep": "see .daedalus/plan.md"}],
                          "count": 3, "flag": True, "nothing": None, "name": ".daedalus"})
    cfg = load(p.path("config.json"))
    live = {}
    for key in LIVE_KEYS:
        rel = cfg[key]
        if key == "knowledge_file":
            data = load(os.path.join(p.root, rel))
            if isinstance(data, dict):
                data["see"] = ".daedalus/specs/stats.json"
            else:
                data = (data or []) + [{"text": "see .daedalus/specs/stats.json"}]
            text = json.dumps(data, indent=2) + "\n"
        else:
            text = read_text(os.path.join(p.root, rel)) + "\nSpecs in .daedalus/specs/, the board in .daedalus/TODO.md.\n"
        p.write(rel, text)
        live[rel] = text
    memory = cfg["memory_dir"] + "/MEMORY.md"
    live[memory] = read_text(os.path.join(p.root, memory)) + "- [q1](q1.md): see .daedalus/research/q1.md\n"
    p.write(memory, live[memory])
    for rel, text in HISTORY.items():
        p.write(f"{OLD}/{rel}", text)
    p.write("tests/test_stats.py", "# covers: STATS-1\n")
    for role in ("builder", "reviewer"):
        p.write(f".codex/agents/daedalus-{role}.toml",
                f'name = "daedalus-{role}"\ndescription = "Daedalus {role}"\n'
                f'developer_instructions = "Work from .daedalus/plan.md."\n')
    p.write("AGENTS.md", OLD_AGENTS_MD)
    p.write(".claude/settings.json", json.dumps({"permissions": {"allow": ["Bash(ls)"]},
                                                 "enabledPlugins": {"daedalus@daedalus": True, "other@x": False}}))
    p.write(".gitignore", "node_modules/\n.daedalus/worktrees/\n.claude/settings.local.json\n")
    sh(p.root, "git", "add", "-A")
    sh(p.root, "git", "commit", "-q", "-m", "work")
    # not committed: local settings (ignored), run state and a log (ignored inside .daedalus), the exclude block
    p.write(".claude/settings.local.json", json.dumps({"enabledPlugins": {"daedalus@my-plugins": False}}))
    p.write(".daedalus/run.json", json.dumps({"active": False, "note": "under .daedalus/"}))
    p.write(".daedalus/hooks.log", "2026-01-01 session start in .daedalus/\n")
    exclude = os.path.join(common_dir(p.root), "info", "exclude")
    before = read_text(exclude) if os.path.exists(exclude) else ""
    save(exclude, before + ("" if before.endswith("\n") or not before else "\n") + OLD_EXCLUDE_TAIL)
    status = git(p.root, "status", "--porcelain")
    assert status.strip() == "", "the rich old project should start with a clean tree:\n" + status
    for rel in ("run.json", "hooks.log"):
        assert git(p.root, "check-ignore", f"{OLD}/{rel}").strip(), f"{OLD}/{rel} should be ignored"
    return p, cfg, live


class MigrateNewProject(unittest.TestCase):
    """MIG-6: a project that already uses .regista/ has nothing to move."""

    def assertNothingToDo(self, root, check):
        before = snapshot(root)
        r = migrate(root, *(["--check"] if check else []))
        out = said(r)
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("nothing to do", out.lower())
        self.assertIn(NEW, out)
        self.assertEqual(snapshot(root), before, "migrate changed something in a project that already uses .regista/")

    def test_migrate_says_nothing_to_do_and_changes_nothing(self):  # covers: MIG-6
        p = Project(gates=[{"name": "tests", "run": "echo ok", "when": ["commit"]}])
        self.addCleanup(p.cleanup)
        self.assertNothingToDo(p.root, check=False)
        self.assertNothingToDo(p.root, check=True)

    def test_a_project_with_both_folders_is_left_alone(self):  # covers: MIG-6
        p = Project(gates=[{"name": "tests", "run": "echo ok", "when": ["commit"]}])
        self.addCleanup(p.cleanup)
        p.commit(".daedalus/config.json", json.dumps({"project": "Old", "plan": ".daedalus/plan.md"}), "old folder")
        self.assertNothingToDo(p.root, check=True)
        self.assertNothingToDo(p.root, check=False)
        self.assertTrue(os.path.isfile(os.path.join(p.root, OLD, "config.json")))

    def test_neither_folder_isnt_a_regista_project(self):  # covers: MIG-6
        root = tempfile.mkdtemp(prefix="regista-plain-")
        self.addCleanup(shutil.rmtree, root, True)
        sh(root, "git", "init", "-q", "-b", "main")
        save(os.path.join(root, "README.md"), "plain\n")
        before = snapshot(root)
        for args in ((), ("--check",)):
            r = migrate(root, *args)
            self.assertEqual(r.returncode, 1, said(r))
            self.assertIn("regista project", said(r).lower())
            self.assertEqual(snapshot(root), before)


class MigrateCheck(unittest.TestCase):
    """MIG-3: migrate --check lists every change and changes nothing."""

    def setUp(self):
        self.p, self.cfg, self.live = rich_old_project()
        self.addCleanup(self.p.cleanup)

    def test_check_lists_every_change(self):  # covers: MIG-3
        before = snapshot(self.p.root)
        r = migrate(self.p.root, "--check")
        out = said(r)
        self.assertEqual(r.returncode, 0, out)
        self.assertIn(OLD, out)  # the folder it moves
        self.assertIn(NEW, out)
        named = ["config.json", "AGENTS.md", ".gitignore", "exclude", "settings.json", "settings.local.json",
                 "daedalus-builder.toml", "daedalus-reviewer.toml", "MEMORY.md"]
        named += [os.path.basename(rel) for rel in self.live]
        for name in named:
            self.assertIn(name, out, f"migrate --check doesn't mention {name}:\n{out}")
        lines = out.splitlines()

        def first_line(name):
            return next(i for i, ln in enumerate(lines) if name in ln)
        separate = ["AGENTS.md", ".gitignore", "exclude", "settings.json", "daedalus-builder.toml"]
        self.assertEqual(len({first_line(n) for n in separate}), len(separate),
                         "each change should be on its own line:\n" + out)
        self.assertEqual(snapshot(self.p.root), before, "migrate --check changed something")

    def test_check_changes_nothing_when_run_twice(self):  # covers: MIG-3
        before = snapshot(self.p.root)
        first = migrate(self.p.root, "--check")
        second = migrate(self.p.root, "--check")
        self.assertEqual((first.returncode, second.returncode), (0, 0), said(first) + said(second))
        self.assertEqual(said(first), said(second))
        self.assertEqual(snapshot(self.p.root), before)
        self.assertTrue(os.path.isfile(self.p.path("config.json")))
        self.assertEqual(folders_named(self.p.root, NEW), [])

    def test_check_gives_the_reasons_it_would_refuse(self):  # covers: MIG-3
        save(self.p.path("fleet", "workers.json"), {"m4": {"state": "running", "milestone": "M4"}})
        self.p.write("README.md", "changed but not committed\n")
        before = snapshot(self.p.root)
        r = migrate(self.p.root, "--check")
        out = said(r)
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("AGENTS.md", out)  # the changes are still listed
        self.assertIn("fleet merge m4", out)
        self.assertIn("fleet remove m4", out)
        self.assertIn("commit", out.lower())
        self.assertEqual(snapshot(self.p.root), before)


class MigrateRefuses(unittest.TestCase):
    """MIG-4: migrate refuses while a worker, a gate run or uncommitted work is in the way, and says what to do."""

    def setUp(self):
        self.p = OldProject(gates=[{"name": "tests", "run": "echo ok", "when": ["commit"]}])
        self.addCleanup(self.p.cleanup)
        self.addCleanup(subprocess.run, ["git", "worktree", "prune"], cwd=self.p.root, capture_output=True)

    def refused(self, cwd=None):
        """Run migrate, check it refused and changed nothing, and return what it said."""
        cwd = cwd or self.p.root
        before, main_before = snapshot(cwd), snapshot(self.p.root)
        r = migrate(cwd)
        out = said(r)
        self.assertEqual(r.returncode, 1, "migrate should have refused:\n" + out)
        self.assertEqual(snapshot(cwd), before, "migrate changed something while refusing:\n" + out)
        self.assertEqual(snapshot(self.p.root), main_before)
        self.assertEqual(folders_named(self.p.root, NEW), [])
        return out

    def assertNamesWorker(self, out, name):
        self.assertIn(f"fleet merge {name}", out)
        self.assertIn(f"fleet remove {name}", out)

    def test_a_folder_under_worktrees(self):  # covers: MIG-4
        self.p.write(".daedalus/worktrees/m2/README.md", "a worker's copy\n")
        self.assertNamesWorker(self.refused(), "m2")

    def test_a_git_worktree_entry_under_worktrees(self):  # covers: MIG-4
        wt = self.p.path("worktrees", "m3")
        sh(self.p.root, "git", "worktree", "add", "-q", wt, "-b", "daedalus/m3")
        shutil.rmtree(wt)  # the folder is gone, but git still lists the worktree
        self.assertIn(wt, git(self.p.root, "worktree", "list"))
        self.assertNamesWorker(self.refused(), "m3")

    def test_a_worker_still_listed_in_the_fleet(self):  # covers: MIG-4
        for state in ("running", "merged", "stopped"):
            with self.subTest(state=state):
                save(self.p.path("fleet", "workers.json"), {"m4": {"state": state, "milestone": "M4"}})
                self.assertNamesWorker(self.refused(), "m4")

    def test_an_unmerged_worker_branch(self):  # covers: MIG-4
        sh(self.p.root, "git", "checkout", "-q", "-b", "daedalus/m5")
        self.p.commit("src/m5.py", "x = 5\n", "feat: m5")
        sh(self.p.root, "git", "checkout", "-q", "main")
        out = self.refused()
        self.assertIn("daedalus/m5", out)
        self.assertIn("m5", out.replace("daedalus/m5", ""))  # the worker, not only the branch
        self.assertIn("merge", out.lower())
        self.assertIn("remove", out.lower())

    def test_a_gate_run_going(self):  # covers: MIG-4
        common = common_dir(self.p.root)
        for lock in (self.p.path("gates", ".lock"), os.path.join(common, "daedalus-gates.lock"),
                     os.path.join(common, "regista-gates.lock")):
            with self.subTest(lock=os.path.basename(os.path.dirname(lock)) + "/" + os.path.basename(lock)):
                os.makedirs(os.path.dirname(lock), exist_ok=True)
                holder = subprocess.Popen([PY, "-c", LOCKER, lock], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          text=True)
                try:
                    self.assertEqual(holder.stdout.readline().strip(), "locked")
                    self.assertIn("wait", self.refused().lower())
                finally:
                    holder.stdin.close()
                    holder.wait(timeout=30)
                    holder.stdout.close()
                r = migrate(self.p.root, "--check")  # once the run is over, nothing blocks
                self.assertEqual(r.returncode, 0, said(r))

    def test_uncommitted_changes_to_tracked_files(self):  # covers: MIG-4
        cases = {
            "changed, not staged": lambda: self.p.write("README.md", "changed\n"),
            "changed and staged": lambda: (self.p.write("README.md", "changed\n"),
                                           sh(self.p.root, "git", "add", "README.md")),
            "a new file staged": lambda: (self.p.write("src/new.py", "x = 1\n"),
                                          sh(self.p.root, "git", "add", "src/new.py")),
            "a harness file changed": lambda: self.p.write(".daedalus/plan.md", "# changed plan\n"),
        }
        for name, make in cases.items():
            with self.subTest(case=name):
                make()
                self.assertIn("commit", self.refused().lower())
                sh(self.p.root, "git", "reset", "-q", "--hard")

    def test_inside_a_worker_worktree(self):  # covers: MIG-4
        save(os.path.join(common_dir(self.p.root), "info", "exclude"), "# Daedalus workers\n.daedalus/worktrees/\n")
        wt = self.p.path("worktrees", "m2")
        sh(self.p.root, "git", "worktree", "add", "-q", wt, "-b", "daedalus/m2")
        save(os.path.join(wt, OLD, "worker.json"), {"name": "m2", "milestone": "M2", "slot": 1,
                                                    "overseer_root": self.p.root, "branch": "daedalus/m2"})
        self.assertIn("main project", self.refused(cwd=wt).lower())
        self.assertEqual(folders_named(wt, NEW), [])
        ctx = context(run_hook(wt, "session-start", {"source": "startup"}))
        self.assertEqual(old_folder_lines(ctx), [], "a worker's session start shouldn't offer the move:\n" + ctx)

    def test_finished_workers_and_untracked_files_dont_block(self):  # covers: MIG-4
        save(self.p.path("fleet", "workers.json"), {"m1": {"state": "removed", "milestone": "M1"}})
        sh(self.p.root, "git", "checkout", "-q", "-b", "daedalus/m1")
        self.p.commit("src/m1.py", "x = 1\n", "feat: m1")
        sh(self.p.root, "git", "checkout", "-q", "main")
        sh(self.p.root, "git", "merge", "-q", "--no-ff", "daedalus/m1", "-m", "merge m1")
        self.p.write("notes.txt", "my own untracked notes\n")
        r = migrate(self.p.root)
        self.assertEqual(r.returncode, 0, said(r))
        self.assertTrue(os.path.isfile(os.path.join(self.p.root, NEW, "config.json")))
        self.assertEqual(read_text(os.path.join(self.p.root, "notes.txt")), "my own untracked notes\n")
        self.assertNotIn("notes.txt", git(self.p.root, "show", "--name-only", "--format=", "HEAD").split())
        self.assertEqual(git(self.p.root, "status", "--porcelain").strip(), "?? notes.txt")


class MigrateMoves(unittest.TestCase):
    """MIG-2 and MIG-5: migrate moves everything in a rich old project and commits it once."""

    @classmethod
    def setUpClass(cls):
        cls.p, cls.cfg, cls.live = rich_old_project()
        p = cls.p
        cls.first = git(p.root, "rev-list", "--max-parents=0", "HEAD").strip()
        cls.head_before = git(p.root, "rev-parse", "HEAD").strip()
        cls.count_before = int(git(p.root, "rev-list", "--count", "HEAD"))
        cls.files_before = {rel: read_bytes(os.path.join(p.root, OLD, *rel.split("/")))
                            for rel in [*HISTORY, "run.json", "hooks.log", "specs/README.md"]}
        cls.result = migrate(p.root)

    @classmethod
    def tearDownClass(cls):
        cls.p.cleanup()

    def new(self, *parts):
        return os.path.join(self.p.root, NEW, *parts)

    def setUp(self):
        self.assertEqual(self.result.returncode, 0, "migrate failed:\n" + said(self.result))

    def test_the_folder_is_moved_with_its_ignored_files(self):  # covers: MIG-2
        self.assertEqual(folders_named(self.p.root, OLD), [])
        self.assertFalse(os.path.exists(os.path.join(self.p.root, OLD)))
        self.assertEqual([f for f in git(self.p.root, "ls-files").splitlines() if f.startswith(OLD + "/")], [])
        for rel in ("plan.md", "status.md", "TODO.md", "knowledge.json", "config.json", "memory/MEMORY.md",
                    "specs/README.md", "state/README.md", ".gitignore", *HISTORY):
            self.assertTrue(os.path.isfile(self.new(*rel.split("/"))), rel)
        self.assertEqual(read_bytes(self.new("run.json")), self.files_before["run.json"])
        self.assertTrue(read_bytes(self.new("hooks.log")).startswith(self.files_before["hooks.log"]))
        self.assertTrue(git(self.p.root, "check-ignore", f"{NEW}/run.json").strip())

    def test_git_sees_renames_and_history_follows(self):  # covers: MIG-2
        changes = git(self.p.root, "show", "-M", "--name-status", "--format=", "HEAD")
        self.assertRegex(changes, r"(?m)^R\d+\t\.daedalus/specs/README\.md\t\.regista/specs/README\.md$", changes)
        self.assertRegex(changes, r"(?m)^R\d+\t\.daedalus/state/2026-01-01-handoff\.md\t\.regista/state/", changes)
        for rel in ("specs/README.md", "state/README.md"):
            log = git(self.p.root, "log", "--follow", "--format=%H", "--", f"{NEW}/{rel}").split()
            self.assertIn(self.first, log, f"git log --follow on {NEW}/{rel} doesn't reach the first commit")

    def test_config_values_point_at_the_new_folder_in_the_same_order(self):  # covers: MIG-2
        after = load(self.new("config.json"))
        expected = moved(self.cfg)
        self.assertEqual(json.dumps(after), json.dumps(expected), "values or key order differ")
        self.assertEqual(after["plan"], ".regista/plan.md")
        self.assertEqual(after["no_gates_for"], ["docs/**", ".regista/research/**"])
        self.assertEqual(after["extra"]["paths"],
                         [".regista/specs/a.json", "src/.daedalus/x", ".daedalusx/y",
                          {"deep": ".regista/research", "keep": "see .daedalus/plan.md"}])
        self.assertEqual(after["extra"]["name"], ".daedalus")

    def test_live_documents_change_and_history_doesnt(self):  # covers: MIG-2
        for rel, text in self.live.items():
            path = os.path.join(self.p.root, NEW + rel[len(OLD):])
            self.assertEqual(read_text(path), text.replace(OLD + "/", NEW + "/"), rel)
        for rel in HISTORY:
            self.assertEqual(read_bytes(self.new(*rel.split("/"))), self.files_before[rel], f"{rel} was changed")
        self.assertEqual(read_bytes(self.new("specs", "README.md")), self.files_before["specs/README.md"])

    def test_codex_agents_are_renamed_and_rewritten(self):  # covers: MIG-2
        agents = os.path.join(self.p.root, ".codex", "agents")
        for role in ("builder", "reviewer"):
            self.assertFalse(os.path.exists(os.path.join(agents, f"daedalus-{role}.toml")), role)
            text = read_text(os.path.join(agents, f"regista-{role}.toml"))
            self.assertIn(f'name = "regista-{role}"', text)
            self.assertIn(f"Regista {role}", text)
            self.assertNotIn("daedalus", text.lower())
            if sys.version_info >= (3, 11):
                import tomllib
                tomllib.loads(text)

    def test_agents_md_gets_the_new_block_once(self):  # covers: MIG-2
        md = read_text(os.path.join(self.p.root, "AGENTS.md"))
        self.assertTrue(md.startswith("# Project rules\nBe kind to the build.\n"), md)
        self.assertTrue(md.rstrip("\n").endswith("## After the block\nKeep this line."), md)
        self.assertEqual(md.count("<!-- regista:start -->"), 1, md)
        self.assertEqual(md.count("<!-- regista:end -->"), 1, md)
        self.assertEqual(md.count("## Regista harness"), 1, md)
        self.assertLess(md.index("<!-- regista:start -->"), md.index("## After the block"))
        self.assertGreater(md.index("<!-- regista:start -->"), md.index("Be kind to the build."))
        self.assertNotIn("daedalus:", md)
        self.assertNotIn("Daedalus harness", md)

    def test_exclude_gets_the_regista_workers_block(self):  # covers: MIG-2
        exclude = read_text(os.path.join(common_dir(self.p.root), "info", "exclude"))
        self.assertEqual(exclude.count("# Regista workers"), 1, exclude)
        self.assertIn(".regista/", exclude.split("# Regista workers", 1)[1])
        self.assertNotIn(".daedalus", exclude)
        self.assertNotIn("Daedalus", exclude)
        self.assertIn("# my own\n*.swp\n", exclude)

    def test_settings_switch_to_the_regista_plugin(self):  # covers: MIG-2
        settings = load(os.path.join(self.p.root, ".claude", "settings.json"))
        self.assertEqual(settings["enabledPlugins"], {"regista@regista": True, "other@x": False})
        self.assertEqual(settings["permissions"], {"allow": ["Bash(ls)"]})
        local = load(os.path.join(self.p.root, ".claude", "settings.local.json"))
        self.assertEqual(local["enabledPlugins"], {"regista@regista": False})

    def test_gitignore_entries_change(self):  # covers: MIG-2
        text = read_text(os.path.join(self.p.root, ".gitignore"))
        self.assertEqual(text, "node_modules/\n.regista/worktrees/\n.claude/settings.local.json\n")

    def test_one_commit_with_the_move_and_a_clean_tree(self):  # covers: MIG-5
        root = self.p.root
        self.assertEqual(int(git(root, "rev-list", "--count", "HEAD")), self.count_before + 1)
        self.assertEqual(git(root, "rev-parse", "HEAD~1").strip(), self.head_before)
        self.assertEqual(git(root, "log", "-1", "--format=%s").strip(), MOVE_MESSAGE)
        self.assertEqual(git(root, "status", "--porcelain").strip(), "")
        committed = set(git(root, "show", "--name-only", "--format=", "HEAD").split())
        for rel in (f"{NEW}/config.json", "AGENTS.md", ".gitignore", ".claude/settings.json",
                    ".codex/agents/regista-builder.toml"):
            self.assertIn(rel, committed)
        self.assertTrue(said(self.result).strip(), "migrate should print a short summary")

    def test_the_tool_works_on_the_new_folder_afterwards(self):  # covers: MIG-2
        root = self.p.root
        ctx = context(run_hook(root, "session-start", {"source": "startup"}))
        self.assertIn("runs on the Regista harness", ctx)
        self.assertIn("Wire the exporter.", ctx)  # the handoff, read from .regista/state
        self.assertEqual(old_folder_lines(ctx), [], ctx)
        r = run_tool(root, "status")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(".regista/status.md", without_tool_path(r.stdout))
        self.assertNotIn(".daedalus/status.md", without_tool_path(r.stdout))
        r = run_tool(root, "gate")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isfile(self.new("gates", "last.json")))
        r = migrate(root, "--check")
        self.assertEqual(r.returncode, 0, said(r))
        self.assertIn("nothing to do", said(r).lower())
        self.assertFalse(os.path.exists(os.path.join(root, OLD)))


class MoveCommitHook(unittest.TestCase):
    """MIG-5: the commit hook treats a move of the harness folder as harness records only, and compares the old
    .daedalus/config.json with the new .regista/config.json for loosened checks."""

    COMMIT = 'git add -A && git commit -m "chore: move the harness folder to .regista"'

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="regista-marker-")
        self.addCleanup(shutil.rmtree, self.out, True)
        self.marker = os.path.join(self.out, "gate-ran")
        self.p = OldProject(gates=[{"name": "tests", "run": f"touch '{self.marker}'; exit 1", "when": ["commit"]},
                                   {"name": "lint", "run": "true", "when": ["commit"]}],
                            protected=["data/**"])
        self.addCleanup(self.p.cleanup)

    def move(self, drop_gate=None):
        """Move the folder by hand: git mv, rewrite the config's .daedalus/ values, stage everything."""
        sh(self.p.root, "git", "mv", OLD, NEW)
        path = os.path.join(self.p.root, NEW, "config.json")
        cfg = moved(load(path))
        if drop_gate:
            cfg["gates"] = [g for g in cfg["gates"] if g["name"] != drop_gate]
        save(path, json.dumps(cfg, indent=2))
        sh(self.p.root, "git", "add", "-A")

    def commit(self):
        return run_hook(self.p.root, "pre-tool", bash(self.COMMIT))

    def test_a_plain_move_runs_no_gates_and_asks_nothing(self):  # covers: MIG-5
        self.move()
        out = self.commit()
        self.assertNotIn(decision(out), ("deny", "ask"), out)
        self.assertNotIn("removed", reason(out))
        self.assertFalse(os.path.exists(self.marker), "a gate ran for a move of the harness folder")

    def test_a_move_with_the_host_files_runs_no_gates(self):  # covers: MIG-5
        self.p.commit("AGENTS.md", OLD_AGENTS_MD, "agents")
        self.move()
        self.p.write("AGENTS.md", "# Project rules\n<!-- regista:start -->\n## Regista harness\n<!-- regista:end -->\n")
        self.p.write(".gitignore", ".regista/worktrees/\n")
        self.p.write(".claude/settings.json", json.dumps({"enabledPlugins": {"regista@regista": True}}))
        self.p.write(".codex/agents/regista-builder.toml", 'name = "regista-builder"\n')
        sh(self.p.root, "git", "add", "-A")
        out = self.commit()
        self.assertNotIn(decision(out), ("deny", "ask"), out)
        self.assertFalse(os.path.exists(self.marker), "a gate ran for a move with only host files beside it")

    def test_a_gate_removed_during_the_move_is_still_reported(self):  # covers: MIG-5
        self.move(drop_gate="lint")
        out = self.commit()
        self.assertEqual(decision(out), "ask", out)
        self.assertIn("lint", reason(out))
        self.assertNotIn("'tests' was removed", reason(out))
        self.assertNotIn("data/**", reason(out))  # the protected list moved unchanged

    def test_a_move_with_other_changes_runs_the_gates(self):  # covers: MIG-5
        self.move()
        self.p.write("src/a.py", "x = 1\n")
        out = self.commit()
        self.assertEqual(decision(out), "deny", out)
        self.assertTrue(os.path.exists(self.marker), "the gates should run when the commit also changes code")

    def test_a_gate_changed_during_the_move_is_still_reported(self):  # covers: MIG-5
        sh(self.p.root, "git", "mv", OLD, NEW)
        path = os.path.join(self.p.root, NEW, "config.json")
        cfg = moved(load(path))
        for g in cfg["gates"]:
            if g["name"] == "lint":
                g["run"] = "echo skipped"
        save(path, json.dumps(cfg, indent=2))
        sh(self.p.root, "git", "add", "-A")
        out = self.commit()
        self.assertEqual(decision(out), "ask", out)
        self.assertIn("lint", reason(out))

    def assertNoLoophole(self, edit):
        """A move whose new config also loosens what counts as records, plus an edit to src/app.py: the gates
        run (the marker appears), or the hook asks about the loosened checks."""
        self.p.commit("src/app.py", "x = 1\n", "app")
        sh(self.p.root, "git", "mv", OLD, NEW)
        path = os.path.join(self.p.root, NEW, "config.json")
        cfg = moved(load(path))
        edit(cfg)
        save(path, json.dumps(cfg, indent=2))
        self.p.write("src/app.py", "x = 2  # changed during the move\n")
        sh(self.p.root, "git", "add", "-A")
        out = self.commit()
        ran = os.path.exists(self.marker)
        self.assertTrue(ran or decision(out) == "ask",
                        f"a code change rode along with the move without gates or a question: {out}")
        self.assertIn(decision(out), ("deny", "ask"), out)

    def test_status_file_pointed_at_code_during_a_move_is_no_loophole(self):  # covers: MIG-5
        self.assertNoLoophole(lambda cfg: cfg.update(status_file="src/app.py"))

    def test_board_file_pointed_at_code_during_a_move_is_no_loophole(self):  # covers: MIG-5
        self.assertNoLoophole(lambda cfg: cfg.update(board_file="src/app.py"))

    def test_no_gates_for_code_added_during_a_move_is_no_loophole(self):  # covers: MIG-5
        self.assertNoLoophole(lambda cfg: cfg.update(no_gates_for=[*cfg.get("no_gates_for", []), "src/**"]))


class MoveSeenFromGit(unittest.TestCase):
    """MIG-5: the hook recognises a move from the last commit and the change set, whatever else is on disk."""

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="regista-marker-")
        self.addCleanup(shutil.rmtree, self.out, True)
        self.marker = os.path.join(self.out, "gate-ran")
        self.p = OldProject(gates=[{"name": "tests", "run": f"touch '{self.marker}'; exit 1", "when": ["commit"]}])
        self.addCleanup(self.p.cleanup)

    def check(self, ignored):
        p = self.p
        p.commit("src/app.py", "x = 1\n", "app")
        old_cfg = read_text(p.path("config.json"))
        sh(p.root, "git", "mv", OLD, NEW)
        path = os.path.join(p.root, NEW, "config.json")
        cfg = moved(load(path))
        cfg["gates"] = []
        save(path, json.dumps(cfg, indent=2))
        p.write("src/app.py", "x = 2\n")
        sh(p.root, "git", "add", "-A")
        p.write(".daedalus/config.json", old_cfg)  # a stray copy of the old config, not part of the change set
        if ignored:
            exclude = os.path.join(common_dir(p.root), "info", "exclude")
            with open(exclude, "a", encoding="utf-8") as f:
                f.write("\n.daedalus/config.json\n")
            self.assertTrue(git(p.root, "check-ignore", ".daedalus/config.json").strip())
        else:
            self.assertIn("?? .daedalus/", git(p.root, "status", "--porcelain"))
        out = run_hook(p.root, "pre-tool", bash("git commit -m x"))
        asked = decision(out) == "ask" and "tests" in reason(out)
        ran = os.path.exists(self.marker) and decision(out) == "deny"
        self.assertTrue(asked or ran, f"a move that drops every gate went through without a question: {out}")

    def test_an_untracked_copy_of_the_old_config_doesnt_hide_the_move(self):  # covers: MIG-5
        self.check(ignored=False)

    def test_an_ignored_copy_of_the_old_config_doesnt_hide_the_move(self):  # covers: MIG-5
        self.check(ignored=True)


class MigrateRollbackIsHonest(unittest.TestCase):
    def test_a_file_the_failed_hook_edited_is_put_back_or_named(self):  # covers: MIG-2
        p, _, _ = rich_old_project()
        self.addCleanup(p.cleanup)
        hook = os.path.join(common_dir(p.root), "hooks", "pre-commit")
        save(hook, "#!/bin/sh\nprintf 'edited by the hook\\n' >> .regista/state/2026-01-01-handoff.md\n"
                   "echo 'the hook says no' >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        before = snapshot(p.root)
        r = migrate(p.root)
        out = said(r)
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("the hook says no", out)
        after = snapshot(p.root)
        changed = sorted(k for k in set(before["files"]) | set(after["files"])
                         if before["files"].get(k) != after["files"].get(k))
        for rel in changed:
            self.assertIn(os.path.basename(rel), out,
                          f"{rel} differs from before, but migrate doesn't say it wasn't put back:\n{out}")
        if not changed:
            self.assertEqual(after, before)


class HalfMovedAdvice(unittest.TestCase):
    def test_the_undo_advice_covers_every_file_migrate_touches(self):  # covers: MIG-2
        p, _, _ = rich_old_project()
        self.addCleanup(p.cleanup)
        # a move cut short after the host files were changed, as migrate would leave it
        sh(p.root, "git", "mv", OLD, NEW)
        p.write("AGENTS.md", "# Project rules\n<!-- regista:start -->\n## Regista harness\n<!-- regista:end -->\n")
        p.write(".gitignore", "node_modules/\n.regista/worktrees/\n.claude/settings.local.json\n")
        p.write(".claude/settings.json", json.dumps({"enabledPlugins": {"regista@regista": True}}))
        p.write(".claude/settings.local.json", json.dumps({"enabledPlugins": {"regista@regista": False}}))
        for role in ("builder", "reviewer"):
            sh(p.root, "git", "mv", f".codex/agents/daedalus-{role}.toml", f".codex/agents/regista-{role}.toml")
        exclude = os.path.join(common_dir(p.root), "info", "exclude")
        save(exclude, read_text(exclude).replace("# Daedalus workers\n.daedalus/", "# Regista workers\n.regista/"))
        r = migrate(p.root)
        out = said(r)
        self.assertEqual(r.returncode, 1, out)
        low = out.lower()
        start = low.find("to undo")
        undo = out[start if start >= 0 else low.find("undo"):]
        for name in ("AGENTS.md", "settings.json", "settings.local.json", ".gitignore", ".codex", "exclude"):
            self.assertIn(name, undo, f"the advice for undoing a half-done move doesn't cover {name}:\n{out}")


class MigrateCommitFails(unittest.TestCase):
    """MIG-2, MIG-5: when the final commit fails, migrate puts everything back as it was and says why."""

    def setUp(self):
        self.p, self.cfg, self.live = rich_old_project()
        self.addCleanup(self.p.cleanup)
        self.hook = os.path.join(common_dir(self.p.root), "hooks", "pre-commit")
        save(self.hook, "#!/bin/sh\necho 'pre-commit refuses: lint the docs first' >&2\nexit 1\n")
        os.chmod(self.hook, 0o755)

    def test_a_failed_commit_leaves_the_project_as_it_was(self):  # covers: MIG-2, MIG-5
        before = snapshot(self.p.root)
        r = migrate(self.p.root)
        out = said(r)
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("commit", out.lower())
        self.assertIn("pre-commit refuses: lint the docs first", out)
        after = snapshot(self.p.root)
        for key in before:
            if key == "files":
                changed = sorted(k for k in set(before["files"]) | set(after["files"])
                                 if before["files"].get(k) != after["files"].get(k))
                self.assertEqual(changed, [], "files not put back as they were")
            else:
                self.assertEqual(after[key], before[key], f"{key} not put back as it was")
        self.assertFalse(os.path.exists(os.path.join(self.p.root, NEW)))
        for rel in ("run.json", "hooks.log"):
            self.assertTrue(os.path.isfile(self.p.path(rel)), rel)
        os.remove(self.hook)
        r = migrate(self.p.root)
        self.assertEqual(r.returncode, 0, said(r))
        self.assertEqual(git(self.p.root, "log", "-1", "--format=%s").strip(), MOVE_MESSAGE)
        self.assertFalse(os.path.exists(os.path.join(self.p.root, OLD)))


class MigrateUnfinishedMove(unittest.TestCase):
    """MIG-4, MIG-6: a move started by hand (or cut short) and not committed isn't 'nothing to do'."""

    def check(self, start_move):
        p = OldProject(gates=[{"name": "tests", "run": "echo ok", "when": ["commit"]}])
        self.addCleanup(p.cleanup)
        start_move(p)
        self.assertTrue(os.path.isdir(os.path.join(p.root, NEW)))
        self.assertIn(f"{OLD}/config.json", git(p.root, "ls-tree", "-r", "--name-only", "HEAD").split())
        before = snapshot(p.root)
        for args in (("--check",), ()):
            r = migrate(p.root, *args)
            out = said(r)
            self.assertEqual(r.returncode, 1, out)
            self.assertNotIn("nothing to do", out.lower())
            self.assertIn("commit", out.lower())  # a move was started and not committed
            self.assertEqual(snapshot(p.root), before)

    def test_a_staged_move_not_committed(self):  # covers: MIG-4, MIG-6
        self.check(lambda p: sh(p.root, "git", "mv", OLD, NEW))

    def test_a_folder_renamed_without_git(self):  # covers: MIG-4, MIG-6
        self.check(lambda p: os.rename(os.path.join(p.root, OLD), os.path.join(p.root, NEW)))


class MigrateCheckFlagsHandFixes(unittest.TestCase):
    def test_check_flags_a_config_value_that_mentions_the_old_folder_mid_text(self):  # covers: MIG-3
        p = OldProject(gates=[{"name": "tests", "run": "bash .daedalus/scripts/check.sh --fast", "when": ["commit"]}])
        self.addCleanup(p.cleanup)
        before = snapshot(p.root)
        r = migrate(p.root, "--check")
        out = said(r)
        flagged = [ln for ln in out.splitlines() if ".daedalus/scripts/check.sh" in ln]
        self.assertTrue(flagged, "migrate --check should flag the gate command that names .daedalus/ mid-text:\n" + out)
        self.assertEqual(snapshot(p.root), before)


SETTINGS_TEXT = ('{\n    "permissions": {\n        "allow": [\n            "Bash(ls)"\n        ]\n    },\n'
                 '    "enabledPlugins": {\n        "other@x": false,\n        "daedalus@daedalus": true,\n'
                 '        "zzz@y": true\n    },\n    "model": "opus"\n}\n')


class MigrateKeepsSettingsFormatting(unittest.TestCase):
    def test_only_the_plugin_key_changes_in_settings(self):  # covers: MIG-2
        p = OldProject(gates=[{"name": "tests", "run": "echo ok", "when": ["commit"]}])
        self.addCleanup(p.cleanup)
        p.commit(".claude/settings.json", SETTINGS_TEXT, "settings")
        r = migrate(p.root)
        self.assertEqual(r.returncode, 0, said(r))
        text = read_text(os.path.join(p.root, ".claude", "settings.json"))
        self.assertIn('"regista@regista": true', text)
        self.assertEqual(text.replace('"regista@regista"', '"daedalus@daedalus"'), SETTINGS_TEXT,
                         "the settings file was reformatted, not just the key switched")


if __name__ == "__main__":
    unittest.main()
