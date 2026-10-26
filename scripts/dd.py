#!/usr/bin/env python3
"""Daedalus core: state, gates and hook decisions for long-running builds in Claude Code and Codex.

Deterministic on purpose. The hooks call this script, so the rules hold even when the model forgets
them. Standard library only.

Project setup lives in `.daedalus/config.json` at the project root (see templates/config.json).
In a folder without one, every hook exits silently, so the plugin can stay enabled everywhere.

CLI (run from anywhere inside the project):
  python dd.py init [--name NAME]         create .daedalus/ from the templates
  python dd.py resume                     where we are / next / blocked, from files and git
  python dd.py status                     resume + gate results + git details
  python dd.py gate [NAME...] [--all] [--accept]
                                          run gates; --all includes manual ones; --accept stores the
                                          passing metrics as the new baseline
  python dd.py snapshot [--reason TEXT]   write a mechanical handoff (git state, recent commits)
  python dd.py handoff-name SLUG          print the path for a new handoff file
  python dd.py context TRANSCRIPT         context use of a session transcript (tokens, window, %)
  python dd.py review-status              does the current change set need a review, and is there one?
  python dd.py review-record              (reviewer agent only) record .daedalus/reviews/draft.json
  python dd.py requirements               requirements, their coverage and pass/fail (set by the gates)
  python dd.py milestone M1               is M1's gate met? exit 0 = done
  python dd.py run [start|end --reason T] the build-all run: start it, end it, or show the milestones left
  python dd.py verify-manual ID --note T  (the user only) record a manual check
  python dd.py try [M1|ID] [--ask]        what to try yourself; --ask (the user, in a terminal) records it
  python dd.py detect                     propose gates, roles and protected paths (JSON)
  python dd.py init --auto [--name NAME]  init + detect + setup, in one go
  python dd.py setup                      the agent hosts' project settings (compaction, Codex agents)
  python dd.py doctor                     does the harness work here? plain language, with fixes
Hooks (stdin = the host's hook JSON; --host codex for Codex, default claude):
  python dd.py hook session-start | pre-tool | user-prompt | user-prompt-expansion | stop |
                    subagent-start | subagent-stop | pre-compact | session-end [--host codex]
                                          (pre-bash and pre-edit are pre-tool's per-tool parts)
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PLUGIN_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DD = os.path.abspath(__file__)

DEFAULTS = {
    "project": "",
    "status_file": ".daedalus/status.md",
    "board_file": ".daedalus/TODO.md",
    "plan": "",
    "state_dir": ".daedalus/state",
    "memory_dir": ".daedalus/memory",
    "knowledge_file": ".daedalus/knowledge.json",
    "gates": [],
    "protected": [],
    "push": "ask",          # ask | block | allow
    "stop": "block",        # block | remind | off: unsaved commits when Claude stops
    "auto_snapshot": True,  # mechanical handoff before compaction and at session end
    # Context budget: at warn_pct the agent is told to reach a safe point and checkpoint. on_limit
    # "compact": the host compacts the session itself at limit_pct (setup writes Claude Code's and
    # Codex's compaction settings) and the work carries on from the saved state; only if the context
    # still passes pause_pct (compaction off or failed) does the old pause apply. on_limit "pause": at
    # limit_pct every tool except saving is refused until the work is saved and the user has the
    # resume prompt. window: tokens, or "auto" (env DAEDALUS_CONTEXT_WINDOW, else 1M once usage passes
    # 200k, else 200k).
    "context": {"warn_pct": 55, "limit_pct": 65, "window": "auto", "on_limit": "compact", "pause_pct": None},
    # orchestrated: the main session coordinates and never edits source or tests itself; the
    # builder, test-writer, reviewer and researcher agents do the work in their own contexts.
    # solo: the main session builds (the review gate still applies if configured).
    # parallel: build ahead of an open milestone gate, but a milestone is done only once its gate
    # passes. strict: a milestone starts only after the previous gate passes.
    "milestones": "parallel",
    "mode": "orchestrated",
    "roles": {"source": ["src/**"], "tests": ["tests/**", "test/**", "**/*_test.*", "**/*.test.*", "**/test_*.*"],
              # files that decide what the tests check: guarded exactly like tests
              "test_config": ["**/conftest.py", "pytest.ini", "tox.ini", ".coveragerc", "**/jest.config.*",
                              "**/vitest.config.*", "**/playwright.config.*", "**/__snapshots__/**", "**/*.snap"]},
    # Requirements live in .daedalus/specs/*.json (written with the plan); whether they pass is decided
    # only by the gates and kept in .daedalus/gates/requirements.json, which no agent can write.
    # A milestone is done only when all its requirements pass and, with milestone_needs_e2e, at least
    # one of them is proven by an end-to-end gate.
    "requirements": {"enforce_coverage": True, "milestone_needs_e2e": True},
    # A commit that changes at least min_changed_lines in these paths needs an approving review of
    # exactly that change set, recorded by the reviewer agent.
    "review": {"required_for": ["src/**"], "min_changed_lines": 15, "max_rounds": 3,
               # build output that tests and tools regenerate: never part of what's reviewed
               "ignore": ["__pycache__/", "*.pyc", "node_modules/", ".pytest_cache/", ".mypy_cache/",
                          ".ruff_cache/", "*.egg-info/", "dist/", "build/", ".venv/", "coverage/", ".coverage"]},
}
# Commits that change only these (besides the harness's own records) skip the gates: there's no code to check.
DEFAULTS["no_gates_for"] = []
# A gate that passed on exactly the current files isn't run again (by the commit hook or `dd.py gate`).
DEFAULTS["reuse_passing_runs"] = True
NESTED = ("context", "roles", "review", "requirements")
HOOK_GATE_BUDGET = 3300  # seconds of gate runs per commit; the hook itself times out at 3600 and would fail open


# --- project and config ---------------------------------------------------------------------

def find_root(start=None):
    d = os.path.abspath(start or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    while True:
        if os.path.isfile(os.path.join(d, ".daedalus", "config.json")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def load_config(root):
    with open(os.path.join(root, ".daedalus", "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    return {**DEFAULTS, **cfg, **{k: {**DEFAULTS[k], **(cfg.get(k) or {})} for k in NESTED}}


def p(root, rel):
    return os.path.join(root, *rel.split("/")) if rel else ""


def read(path, limit=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    return text if limit is None or len(text) <= limit else text[:limit] + "\n…(truncated)"


# --- git ------------------------------------------------------------------------------------

def git(root, *args, strip=True):
    r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return (r.stdout.strip() if strip else r.stdout) if r.returncode == 0 else ""


def changed_files(root):
    """Staged, unstaged and untracked paths: a superset of what a commit could include, because a
    hook sees `git add -A && git commit` before the add has run."""
    out = git(root, "status", "--porcelain", "--untracked-files=all", strip=False)  # " M x": the space matters
    files = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path.strip('"').replace("\\", "/"))
    return files


def ahead_count(root):
    n = git(root, "rev-list", "--count", "@{u}..HEAD")
    return n if n.isdigit() else "?"


# --- globs ----------------------------------------------------------------------------------

def glob_re(pattern):
    """Gitignore-like globs: `**` crosses folders, `*` and `?` don't. A pattern without a slash
    matches at any depth; a trailing slash or /** means everything under a folder."""
    pat = pattern.strip().replace("\\", "/")
    if pat.endswith("/"):
        pat += "**"
    anchored = "/" in pat.rstrip("/")
    pat = pat.lstrip("/")
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pat.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pat[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pat[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pat[i]), i + 1
    return re.compile(("" if anchored else "(?:.*/)?") + out + "$")


def matches(path, patterns):
    path = path.replace("\\", "/")
    while path.startswith("./"):  # not lstrip("./"): that also eats the dot of ".env"
        path = path[2:]
    path = path.lstrip("/")
    return [pt for pt in patterns if glob_re(pt).match(path)]


def rel_to_root(root, path):
    ap = os.path.abspath(os.path.join(root, path)) if not os.path.isabs(path) else os.path.abspath(path)
    try:
        rel = os.path.relpath(ap, root)
    except ValueError:  # another drive on Windows
        return None
    return None if rel.startswith("..") else rel.replace("\\", "/")


# --- shell for gate commands ----------------------------------------------------------------

def find_bash():
    if os.environ.get("DAEDALUS_BASH"):
        return os.environ["DAEDALUS_BASH"]
    if os.name != "nt":
        return shutil.which("bash")
    gitexe = shutil.which("git")
    if gitexe:  # Git for Windows: ...\Git\cmd\git.exe → ...\Git\bin\bash.exe
        cand = os.path.join(os.path.dirname(os.path.dirname(gitexe)), "bin", "bash.exe")
        if os.path.isfile(cand):
            return cand
    b = shutil.which("bash")
    return b if b and "system32" not in b.lower() else None  # System32\bash.exe is WSL


def run_shell(cmd, cwd, timeout):
    bash = find_bash()
    argv = [bash, "-lc", cmd] if bash else cmd
    try:
        r = subprocess.run(argv, cwd=cwd, shell=not bash, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        return 124, out + f"\n[daedalus] timed out after {timeout} s"


# --- gates ----------------------------------------------------------------------------------

def last_json(text):
    """The last JSON object printed by a gate command (one line, or a final {...} block)."""
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    m = re.search(r"(\{[^{}]*\})\s*$", text.strip(), re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except ValueError:
            pass
    return None


def baseline_path(root):
    return p(root, ".daedalus/gates/baseline.json")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def check_metrics(gate, values, baseline):
    """Thresholds and the no-regression rule. Returns a list of failure strings."""
    fails = []
    for name, rule in (gate.get("metrics") or {}).items():
        if name not in values:
            fails.append(f"{name}: missing from the gate's JSON output")
            continue
        v = values[name]
        if not isinstance(v, (int, float)):
            fails.append(f"{name}: not a number ({v!r})")
            continue
        if "max" in rule and v > rule["max"]:
            fails.append(f"{name} = {v} > max {rule['max']}")
        if "min" in rule and v < rule["min"]:
            fails.append(f"{name} = {v} < min {rule['min']}")
        better = rule.get("better") or ("lower" if "max" in rule else "higher" if "min" in rule else None)
        base = (baseline.get(gate["name"]) or {}).get("metrics", {}).get(name)
        if rule.get("no_worse", True) and better and isinstance(base, (int, float)):
            tol = rule.get("tolerance", 0)
            worse = v > base + tol if better == "lower" else v < base - tol
            if worse:
                fails.append(f"{name} = {v} is worse than the baseline {base} (tolerance {tol})")
    return fails


def select_gates(cfg, names=None, event=None, include_manual=False, files=None):
    out = []
    for g in cfg["gates"]:
        if names and g["name"] not in names:
            continue
        when = g.get("when", ["commit"])
        when = [when] if isinstance(when, str) else when
        if not names:
            if event and event not in when:
                continue
            if not event and "manual" in when and not include_manual and "commit" not in when:
                continue
        if files is not None and g.get("paths") and not any(matches(f, g["paths"]) for f in files):
            out.append({**g, "_skip": "no changed file matches its paths"})
            continue
        out.append(g)
    return out


def state_fingerprint(root, cfg):
    """sha256 over the content gates can see: the committed files and the uncommitted changes, without
    the harness's own records (status, board, specs, reviews), so writing those doesn't void a run."""
    import hashlib
    h = hashlib.sha256()
    for ln in git(root, "ls-tree", "-r", "HEAD").splitlines():
        meta, _, path = ln.partition("\t")
        if not bookkeeping(cfg, path):
            h.update(path.encode() + b"\0" + meta.encode() + b"\n")
    h.update(b"--changes--")
    for f in sorted(changed_files(root)):
        if bookkeeping(cfg, f):
            continue
        h.update(f.encode() + b"\0")
        try:
            with open(p(root, f), "rb") as fh:
                h.update(hashlib.sha256(fh.read()).digest())
        except OSError:
            h.update(b"<deleted>")
    return h.hexdigest()[:20]


def passed_path(root):
    return p(root, ".daedalus/gates/passed.json")


def gate_lock(root, wait, note=None):
    """An exclusive lock on the project's gate runs (released when the process ends), so two runs never
    share a test database or a port. Waits up to `wait` seconds; returns the open lock file or None."""
    path = p(root, ".daedalus/gates/.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a+")
    deadline, told = time.time() + wait, False
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if time.time() >= deadline:
                fh.close()
                return None
            if note and not told:
                print(note, file=sys.stderr)
                told = True
            time.sleep(1)


def run_gates(root, cfg, gates, accept=False, budget=None, reuse=None):
    """Run gates in order, one run per project at a time. A gate that already passed on exactly these
    files is reused instead of run again (unless `reuse` is False). With `budget` (seconds), waiting for
    the lock and each gate's timeout are cut to what's left, and a gate with no time left fails: the
    commit hook must decide before its own timeout lets the action through."""
    started = time.time()
    lock = gate_lock(root, (budget - 60) if budget else 4 * 3600,
                     note="Daedalus: another gate run is going in this project; waiting for it to finish.")
    if lock is None:
        return [{"name": g["name"], "status": "fail", "seconds": 0,
                 "fails": ["another gate run in this project held the lock for the whole time budget"],
                 "tail": "Wait for the other run (an agent running the gates), then try again."} for g in gates]
    try:
        return _run_gates(root, cfg, gates, accept, budget, started,
                          cfg.get("reuse_passing_runs", True) if reuse is None else reuse)
    finally:
        lock.close()


def _run_gates(root, cfg, gates, accept, budget, started, reuse):
    baseline = load_json(baseline_path(root), {})
    fp = state_fingerprint(root, cfg)
    passed = load_json(passed_path(root), {})
    earlier = (passed.get(fp) or {}).get("gates", {}) if reuse else {}
    results = []
    for g in gates:
        if g.get("_skip"):
            results.append({"name": g["name"], "status": "skipped", "why": g["_skip"]})
            continue
        prev = earlier.get(g["name"])
        if prev and prev.get("run") == g["run"] and not accept:
            if not (g.get("metrics") and check_metrics(g, prev.get("metrics") or {}, baseline)):
                results.append({**prev["result"], "reused": prev["at"]})
                continue
        timeout = g.get("timeout", 600)
        if budget is not None:
            left = budget - (time.time() - started)
            if left < 5:
                results.append({"name": g["name"], "status": "fail", "seconds": 0,
                                "fails": [f"no time left in the {budget} s gate budget of the commit hook"],
                                "tail": "Make the commit gates faster, or move slow ones to when: [\"manual\"]."})
                continue
            timeout = min(timeout, int(left))
        t0 = time.time()
        code, out = run_shell(g["run"], root, timeout)
        res = {"name": g["name"], "exit": code, "seconds": round(time.time() - t0, 1)}
        fails = [] if code == 0 else [f"command exited {code}"]
        if g.get("metrics"):
            values = last_json(out) or {}
            res["metrics"] = {k: values.get(k) for k in g["metrics"]}
            if code == 0:
                fails += check_metrics(g, values, baseline)
        res["status"] = "fail" if fails else "pass"
        if fails:
            res["fails"] = fails
            res["tail"] = "\n".join(out.strip().splitlines()[-25:])
        results.append(res)
    record = {"date": dt.datetime.now().isoformat(timespec="seconds"),
              "commit": git(root, "rev-parse", "--short", "HEAD"), "results": results}
    save_json(p(root, ".daedalus/gates/last.json"), record)
    with open(p(root, ".daedalus/gates/history.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    fresh = [r for r in results if r["status"] == "pass" and not r.get("reused")]
    if fresh and state_fingerprint(root, cfg) == fp:  # a run that changed the files proves nothing about them
        runs = {g["name"]: g["run"] for g in gates}
        entry = passed.get(fp) or {"gates": {}}
        for r in fresh:
            entry["gates"][r["name"]] = {"run": runs[r["name"]], "at": record["date"], "result": r,
                                         "metrics": r.get("metrics")}
        entry["at"] = record["date"]
        passed[fp] = entry
        save_json(passed_path(root), dict(sorted(passed.items(), key=lambda kv: kv[1]["at"])[-20:]))
    if accept:
        for r in results:
            if r["status"] == "pass" and r.get("metrics"):
                baseline[r["name"]] = {"metrics": r["metrics"], "date": record["date"], "commit": record["commit"]}
        save_json(baseline_path(root), baseline)
    return results


def format_results(results):
    lines = []
    for r in results:
        if r["status"] == "skipped":
            lines.append(f"- {r['name']}: skipped ({r['why']})")
            continue
        m = ", ".join(f"{k}={v}" for k, v in (r.get("metrics") or {}).items())
        again = f", reused: it passed at {r['reused'][11:16]} on exactly these files" if r.get("reused") else ""
        lines.append(f"- {r['name']}: {r['status'].upper()} in {r['seconds']} s{again}" + (f" ({m})" if m else ""))
        for fl in r.get("fails", []):
            lines.append(f"    · {fl}")
    return "\n".join(lines)


# --- state ----------------------------------------------------------------------------------

def handoffs(root, cfg):
    files = glob.glob(os.path.join(p(root, cfg["state_dir"]), "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-*.md"))
    return sorted(files, key=os.path.getmtime)


def newest_handoff(root, cfg, real_only=False):
    hs = [h for h in handoffs(root, cfg) if not (real_only and "-auto-" in os.path.basename(h))]
    return hs[-1] if hs else None


def bookkeeping(cfg, path):
    """Files the save step itself writes: committing them is not new work."""
    path = path.replace("\\", "/")
    return path.startswith(".daedalus/") or path in (cfg["status_file"], cfg["board_file"])


HOST_FILES = (".claude/settings.json", "AGENTS.md")


def harness_only(root, cfg, files):
    """Does this change touch only the harness's own records (status, board, specs, reviews, the setup
    files `dd.py init` writes) or `no_gates_for` paths? Then there's no code for the gates to check. The
    config counts only while it's new: once committed, a config change runs the gates like code."""
    if not files:
        return False
    config_new = not git(root, "ls-files", "--", ".daedalus/config.json")
    for f in files:
        if f == ".daedalus/config.json":
            if not config_new:
                return False
        elif not (bookkeeping(cfg, f) or f in HOST_FILES or f.startswith(".codex/")
                  or matches(f, cfg.get("no_gates_for") or [])):
            return False
    return True


def config_loosened(root, cfg):
    """What the working config loosens compared with the last commit's (plain-language list). Adding a
    gate or a protected path is fine; removing or changing one, or switching a check off, isn't."""
    try:
        old = json.loads(git(root, "show", "HEAD:.daedalus/config.json") or "null")
    except ValueError:
        return []
    if not isinstance(old, dict):
        return []
    old = {**DEFAULTS, **old, **{k: {**DEFAULTS[k], **(old.get(k) or {})} for k in NESTED}}
    out = []
    new_gates = {g.get("name"): g for g in cfg["gates"]}
    for g in old["gates"]:
        if g.get("name") not in new_gates:
            out.append(f"the gate '{g.get('name')}' was removed")
        elif new_gates[g.get("name")] != g:
            out.append(f"the gate '{g.get('name')}' was changed")

    def dropped(label, before, after):
        gone = [x for x in before or [] if x not in (after or [])]
        if gone:
            out.append(f"{label} no longer include{'s' if len(gone) == 1 else ''} {', '.join(map(str, gone[:5]))}")
    dropped("the protected paths", old["protected"], cfg["protected"])
    for k in ("source", "tests", "test_config"):
        dropped(f"the {k.replace('_', ' ')} paths", old["roles"].get(k), cfg["roles"].get(k))
    dropped("the paths that need a review", old["review"].get("required_for"), cfg["review"].get("required_for"))
    added = [x for x in cfg.get("no_gates_for") or [] if x not in (old.get("no_gates_for") or [])]
    if added:
        out.append(f"commits of {', '.join(added)} now skip the gates")
    if cfg["review"].get("min_changed_lines", 0) > old["review"].get("min_changed_lines", 0):
        out.append("bigger changes now go without a review")
    for k, label in (("enforce_coverage", "requirements no longer need a test naming them"),
                     ("milestone_needs_e2e", "milestones no longer need end-to-end proof")):
        if old["requirements"].get(k, True) and not cfg["requirements"].get(k, True):
            out.append(label)
    if old["mode"] == "orchestrated" and cfg["mode"] != "orchestrated":
        out.append("the main session may now edit code itself (solo mode)")
    order = {"block": 0, "ask": 1, "allow": 2}
    if order.get(cfg["push"], 2) > order.get(old["push"], 2):
        out.append(f"pushing went from '{old['push']}' to '{cfg['push']}'")
    return out


def commits_since(root, cfg, since):
    """Work commits newer than `since` (epoch seconds), newest first, as 'hash subject'."""
    out = []
    for ln in git(root, "log", "--format=%h %ct %s", "-30").splitlines():
        parts = ln.split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit() or int(parts[1]) <= since:
            continue
        files = git(root, "show", "--name-only", "--format=", parts[0]).splitlines()
        if files and all(bookkeeping(cfg, f) for f in files):
            continue
        out.append(parts[0] + " " + (parts[2] if len(parts) > 2 else ""))
    return out


def unsaved_commits(root, cfg):
    """Work commits made after the newest real handoff was written."""
    h = newest_handoff(root, cfg, real_only=True)
    return commits_since(root, cfg, os.path.getmtime(h) if h else 0)


def section(md, title):
    m = re.search(r"^#+\s*" + re.escape(title) + r"[^\n]*\n(.*?)(?=^#+\s|\Z)", md, re.S | re.M | re.I)
    return m.group(1).strip() if m else ""


def resume_text(root, cfg, detailed=False):
    name = cfg["project"] or os.path.basename(root)
    h = newest_handoff(root, cfg, real_only=True)
    auto = newest_handoff(root, cfg)
    hand = read(h) if h else ""
    status = read(p(root, cfg["status_file"]), 6000)
    board = read(p(root, cfg["board_file"]), 4000)
    last = load_json(p(root, ".daedalus/gates/last.json"), {})
    dirty = changed_files(root)
    parts = [f"# Daedalus: {name}",
             f"Root: {root}",
             f"Daedalus CLI: python \"{DD}\" <resume|status|gate|snapshot|handoff-name> (run from the project)",
             ""]
    parts.append("## Latest handoff" + (f" ({os.path.relpath(h, root)})" if h else ""))
    if hand:
        for t in ("Resume point", "Blocked on", "In flight"):
            s = section(hand, t)
            if s:
                parts.append(f"**{t}:** {s}")
    else:
        parts.append("No handoff yet.")
    if auto and auto != h:
        parts.append(f"(A newer automatic snapshot exists: {os.path.relpath(auto, root)}. The last session "
                     "ended or compacted without a save; read it and check git before trusting the handoff.)")
    nxt = section(status, "Next")
    if nxt:
        parts.append(f"\n## Next (from {cfg['status_file']})\n{nxt[:1500]}")
    changes = changes_text(root)
    if changes:
        parts.append("\n## Changes from the user, not in the plan yet\nFold each into the plan (milestone, steps, "
                     f"spec), then `python \"{DD}\" change done <n> --where <milestone>`.\n" + changes)
    open_items = [ln for ln in board.splitlines() if re.match(r"\s*[-*]\s*(⬜|🔄|\[ \])", ln)]
    if open_items:
        parts.append("\n## Open on the board\n" + "\n".join(open_items[:12]))
    parts.append("\n## Git")
    parts.append(f"- {git(root, 'rev-parse', '--abbrev-ref', 'HEAD')} @ {git(root, 'rev-parse', '--short', 'HEAD')}, "
                 f"{'clean' if not dirty else f'{len(dirty)} uncommitted file(s)'}, {ahead_count(root)} commit(s) not pushed")
    unsaved = unsaved_commits(root, cfg)
    if unsaved:
        parts.append(f"- {len(unsaved)} commit(s) since the last handoff: " + "; ".join(unsaved[:5]))
    if last:
        parts.append(f"\n## Last gate run ({last.get('date')}, {last.get('commit')})\n" + format_results(last["results"]))
    reqs = requirements_summary(root, cfg)
    if reqs:
        parts.append("\n## Requirements and milestones (decided by the gates, not by notes)\n" + reqs)
    run = load_json(run_path(root), {})
    if run.get("active"):
        parts.append(f"\n## Build-all run\nActive since {run.get('started')}. After a compaction or "
                     f"`{cmd_name('start')} continue`, carry on with `{cmd_name('build')} all` without waiting. In a new "
                     f"session, wait for the user's command; `python \"{DD}\" run` shows where it stands.")
    if detailed:
        parts.append("\n## Recent commits\n" + git(root, "log", "--oneline", "-8"))
        if dirty:
            parts.append("\n## Uncommitted\n" + "\n".join(dirty[:30]))
        parts.append("\n## Gates\n" + "\n".join(
            f"- {g['name']}: `{g['run']}` when={g.get('when', ['commit'])}" + (f" paths={g['paths']}" if g.get("paths") else "")
            for g in cfg["gates"]) if cfg["gates"] else "\n## Gates\nNone configured.")
        if cfg["protected"]:
            parts.append("\n## Protected paths\n" + ", ".join(cfg["protected"]))
    return "\n".join(parts)


def snapshot(root, cfg, reason):
    now = dt.datetime.now()
    path = os.path.join(p(root, cfg["state_dir"]), f"{now:%Y-%m-%d}-auto-{now:%H%M%S}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    dirty = changed_files(root)
    body = f"""# Daedalus handoff — {now:%Y-%m-%d} (automatic snapshot)

Written by the harness ({reason}), not by a save. It records git state only; the next session should
check it against the conversation-free facts (git, status file) and write a proper handoff.

## Repo state
- {git(root, 'rev-parse', '--abbrev-ref', 'HEAD')} @ {git(root, 'rev-parse', '--short', 'HEAD')}, {ahead_count(root)} commit(s) not pushed
- uncommitted: {', '.join(dirty[:20]) if dirty else 'none'}

## Commits since the last real handoff
{chr(10).join('- ' + c for c in unsaved_commits(root, cfg)) or '- none'}

## Resume point
Unknown: the session didn't save. Read the status file's Next section and the commits above.
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def needs_snapshot(root, cfg):
    h = newest_handoff(root, cfg)  # any handoff, auto included: don't stack snapshots
    since = os.path.getmtime(h) if h else 0
    if commits_since(root, cfg, since):
        return True
    for f in changed_files(root):
        fp = p(root, f)
        if os.path.exists(fp) and os.path.getmtime(fp) > since and not bookkeeping(cfg, f):
            return True
    return False


# --- review gate ----------------------------------------------------------------------------

def reviewed_files(root, cfg):
    """Changed files the review rule covers, sorted."""
    pats = cfg["review"].get("required_for") or []
    ignore = cfg["review"].get("ignore") or []
    return sorted(f for f in changed_files(root) if pats and matches(f, pats) and not matches(f, ignore))


def change_size(root, files):
    """Lines added + removed across `files` vs HEAD (untracked files count all their lines)."""
    total = 0
    tracked = set(git(root, "ls-files", "--", *files).splitlines()) if files else set()
    for f in files:
        if f in tracked:
            for ln in git(root, "diff", "HEAD", "--numstat", "--", f).splitlines():
                a, b, *_ = ln.split("\t") + ["", ""]
                total += (int(a) if a.isdigit() else 0) + (int(b) if b.isdigit() else 0)
        else:
            try:
                with open(p(root, f), encoding="utf-8", errors="replace") as fh:
                    total += sum(1 for _ in fh)
            except OSError:
                pass
    return total


def fingerprint(root, files):
    """sha256 over each file's path and current content: any edit after the review changes it."""
    import hashlib
    h = hashlib.sha256()
    for f in files:
        fp = p(root, f)
        h.update(f.encode() + b"\0")
        try:
            with open(fp, "rb") as fh:
                h.update(hashlib.sha256(fh.read()).digest())
        except OSError:
            h.update(b"<deleted>")
    return h.hexdigest()[:16]


def review_needed(root, cfg):
    """(files, fingerprint) when the current change set needs a review, else (None, None)."""
    files = reviewed_files(root, cfg)
    if not files or change_size(root, files) < cfg["review"].get("min_changed_lines", 0):
        return None, None
    return files, fingerprint(root, files)


def review_path(root, fp):
    return p(root, f".daedalus/reviews/{fp}.json")


def review_status(root, cfg):
    """None if no review is needed or an approving one matches; else a reason string."""
    files, fp = review_needed(root, cfg)
    if not files:
        return None
    rec = load_json(review_path(root, fp), None)
    if rec is None:
        return (f"Daedalus: this change ({len(files)} file(s) under {', '.join(cfg['review']['required_for'])}, "
                f"fingerprint {fp}) has no review. Spawn the daedalus reviewer agent on the current changes; it "
                "records its verdict with `dd.py review-record`. Any edit after the review needs a new review.")
    if rec.get("verdict") != "approve":
        return (f"Daedalus: the reviewer asked for changes on this exact change set ({fp}): "
                + "; ".join(f"{x.get('file')}:{x.get('line')} {x.get('issue')}" for x in rec.get("findings", [])[:5])
                + ". Send the findings to a fresh builder agent, then review again.")
    return None


REVIEW_DRAFT = ".daedalus/reviews/draft.json"


def review_record(root, cfg):
    """Validate the reviewer's draft and stamp it with the fingerprint of the current change set."""
    draft = load_json(p(root, REVIEW_DRAFT), None)
    if draft is None:
        sys.exit(f"Write your verdict to {REVIEW_DRAFT} first (see the reviewer agent's format).")
    problems = []
    if draft.get("verdict") not in ("approve", "changes"):
        problems.append('verdict must be "approve" or "changes"')
    if not draft.get("checked"):
        problems.append("checked must list what you verified (spec items, edge cases, gates, rules)")
    findings = draft.get("findings") or []
    for i, f in enumerate(findings):
        if not (f.get("file") and f.get("line") and f.get("issue") and f.get("severity") in ("blocker", "major", "minor")):
            problems.append(f"findings[{i}] needs file, line, issue and severity (blocker|major|minor)")
    if draft.get("verdict") == "approve" and any(f.get("severity") in ("blocker", "major") for f in findings):
        problems.append("an approval can't carry blocker or major findings")
    if draft.get("verdict") == "changes" and not findings:
        problems.append("asking for changes needs at least one finding")
    if problems:
        sys.exit("Review not recorded:\n- " + "\n- ".join(problems))
    files = reviewed_files(root, cfg)
    if not files:
        sys.exit("Nothing to review: no changed files under review.required_for.")
    fp = fingerprint(root, files)
    rec = {**draft, "fingerprint": fp, "files": files, "at": dt.datetime.now().isoformat(timespec="seconds"),
           "base": git(root, "rev-parse", "--short", "HEAD")}
    save_json(review_path(root, fp), rec)
    os.remove(p(root, REVIEW_DRAFT))
    print(f"Recorded review {rec['verdict']} for {len(files)} file(s), fingerprint {fp}.")


# --- requirements ---------------------------------------------------------------------------
#
# .daedalus/specs/<step>.json, written by the orchestrator when a step starts:
#   {"step": "stats", "milestone": "M1", "title": "...", "state": "active" | "planned",
#    "requirements": [{"id": "STATS-1", "text": "WHEN the list is empty mean SHALL raise ValueError",
#                      "verify": "test" | "e2e" | "manual"}]}
# A requirement is covered when a test file names its ID (e.g. a `# covers: STATS-1` comment):
# "test" in any test file, "e2e" in a file under an e2e gate's `tests` globs. It passes when it is
# covered and every gate that proves it passed in the same run. "manual" passes only through
# `dd.py verify-manual`, which no agent may run. Results: .daedalus/gates/requirements.json.

REQ_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.]*-\d+$")
VERIFY = ("test", "e2e", "manual")


def requirements_path(root):
    return p(root, ".daedalus/gates/requirements.json")


def load_specs(root):
    """(specs, problems). Each spec gains `_file`."""
    specs, problems = [], []
    for f in sorted(glob.glob(p(root, ".daedalus/specs/*.json"))):
        name = os.path.relpath(f, root).replace("\\", "/")
        try:
            with open(f, encoding="utf-8") as fh:
                s = json.load(fh)
        except (OSError, ValueError) as e:
            problems.append(f"{name}: not valid JSON ({e})")
            continue
        s["_file"] = name
        specs.append(s)
    seen = {}
    for s in specs:
        for r in s.get("requirements") or []:
            rid = r.get("id", "")
            if not REQ_ID.match(rid):
                problems.append(f"{s['_file']}: requirement id {rid!r} must look like NAME-1")
            elif rid in seen:
                problems.append(f"{s['_file']}: {rid} is also defined in {seen[rid]}")
            else:
                seen[rid] = s["_file"]
            if r.get("verify", "test") not in VERIFY:
                problems.append(f"{s['_file']}: {rid} verify must be one of {', '.join(VERIFY)}")
            if not (r.get("text") or "").strip():
                problems.append(f"{s['_file']}: {rid} has no text")
    return specs, problems


def e2e_gates(cfg):
    return [g for g in cfg["gates"] if g.get("evidence") == "e2e"]


def files_matching(root, globs):
    """Tracked and untracked (not ignored) files matching globs."""
    out = set(git(root, "ls-files").splitlines())
    out |= set(git(root, "ls-files", "--others", "--exclude-standard").splitlines())
    return sorted(f for f in out if globs and matches(f, globs))


def coverage(root, cfg, specs):
    """{req id: {"tests": [files naming it], "e2e": [e2e gate names whose test files name it]}}"""
    test_files = files_matching(root, cfg["roles"]["tests"])
    texts = {}
    for f in set(test_files) | {x for g in e2e_gates(cfg) for x in files_matching(root, g.get("tests") or [])}:
        try:
            with open(p(root, f), encoding="utf-8", errors="replace") as fh:
                texts[f] = fh.read()
        except OSError:
            pass
    cov = {}
    for s in specs:
        for r in s.get("requirements") or []:
            rid = r.get("id", "")
            pat = re.compile(r"(?<![A-Za-z0-9_.-])" + re.escape(rid) + r"(?![0-9])")
            cov[rid] = {"tests": [f for f in test_files if pat.search(texts.get(f, ""))],
                        "e2e": [g["name"] for g in e2e_gates(cfg)
                                if any(pat.search(texts.get(f, "")) for f in files_matching(root, g.get("tests") or []))]}
    return cov


def active(spec):
    return spec.get("state", "active") != "planned"


def coverage_problems(root, cfg):
    """What stops a commit: invalid specs, and active requirements no test names."""
    specs, problems = load_specs(root)
    if not cfg["requirements"].get("enforce_coverage", True):
        return problems
    cov = coverage(root, cfg, specs)
    for s in specs:
        if not active(s):
            continue
        for r in s.get("requirements") or []:
            v, rid = r.get("verify", "test"), r.get("id", "")
            c = cov.get(rid, {})
            if v == "test" and not c.get("tests"):
                problems.append(f"{rid} ({s['_file']}): no test names it. The test writer adds `covers: {rid}` to the test.")
            if v == "e2e" and not c.get("e2e"):
                where = ", ".join(f"{g['name']} ({', '.join(g.get('tests') or [])})" for g in e2e_gates(cfg)) or \
                    "none configured: add a gate with \"evidence\": \"e2e\" and its \"tests\" globs"
                problems.append(f"{rid} ({s['_file']}): needs an end-to-end test naming it, run by an e2e gate: {where}.")
    return problems


def record_requirements(root, cfg, results):
    """After a gate run on committed code (the commit hook's, or a run on a clean tree), the harness
    alone updates which requirements pass. An entry is rewritten only when its result changes, so the
    file doesn't churn with every commit."""
    specs, _ = load_specs(root)
    cov = coverage(root, cfg, specs)
    status = {r["name"]: r["status"] for r in results}
    commit_gates_ok = all(r["status"] != "fail" for r in results)
    e2e_names = {g["name"] for g in e2e_gates(cfg)}
    tests_ran = any(r["name"] not in e2e_names and r["status"] in ("pass", "fail") for r in results)
    before = load_json(requirements_path(root), {})
    state = json.loads(json.dumps(before))
    now = dt.datetime.now().isoformat(timespec="seconds")
    for s in specs:
        for r in s.get("requirements") or []:
            rid, v = r.get("id", ""), r.get("verify", "test")
            prev = state.get(rid, {})
            keep = {**prev, "verify": v, "milestone": s.get("milestone"), "spec": s["_file"],
                    "passes": bool(prev.get("passes"))}
            c = cov.get(rid, {})
            if v == "manual" or (v == "test" and not tests_ran):  # manual: only the user; unit tests didn't run
                state[rid] = keep
                continue
            if v == "test":
                ok = bool(c.get("tests")) and commit_gates_ok
                evidence = c.get("tests", [])
            else:
                ran = [g for g in c.get("e2e", []) if g in status]
                if not ran:  # its e2e gate didn't run this time: keep the last verdict
                    state[rid] = keep
                    continue
                ok = all(status[g] == "pass" for g in ran) and commit_gates_ok
                evidence = ran
            if prev.get("passes") == ok and prev.get("evidence") == evidence and prev.get("at"):
                state[rid] = keep
                continue
            state[rid] = {"verify": v, "milestone": s.get("milestone"), "spec": s["_file"], "passes": ok,
                          "evidence": evidence, "at": now, "base": git(root, "rev-parse", "--short", "HEAD")}
    if state != before:
        save_json(requirements_path(root), state)
    return state


def work_changes(root, cfg):
    """Uncommitted changes the gates can see (not the harness's own records)."""
    return [f for f in changed_files(root) if not bookkeeping(cfg, f)]


def milestone_status(root, cfg, milestone):
    """(done, reasons): done only when every requirement of the milestone passes (by the gates)
    and, if configured, at least one is proven end to end."""
    specs, problems = load_specs(root)
    state = load_json(requirements_path(root), {})
    reqs = [(s, r) for s in specs if s.get("milestone") == milestone for r in s.get("requirements") or []]
    reasons = list(problems)
    if not reqs:
        reasons.append(f"no requirements are specified for {milestone} in .daedalus/specs/")
    for s, r in reqs:
        if not state.get(r["id"], {}).get("passes"):
            reasons.append(f"{r['id']} doesn't pass yet ({r.get('verify', 'test')})")
    if cfg["requirements"].get("milestone_needs_e2e", True) and reqs and not any(
            r.get("verify") == "e2e" and state.get(r["id"], {}).get("passes") for _, r in reqs):
        reasons.append(f"no end-to-end requirement of {milestone} passes: a milestone needs behavioural evidence, "
                       "not only unit tests")
    return not reasons, reasons


# --- build all: one run through every milestone ---------------------------------------------
#
# `/daedalus:build all` builds the plan milestone after milestone. `dd.py run start` marks the run
# active in .daedalus/run.json (local); while it is, the Stop hook sends the session back to work
# instead of letting the turn end, as long as each stop comes after new commits. A run that stops
# twice without committing anything ends itself, so a stuck run can't trap the session.
# `dd.py run end --reason …` ends it: everything is done, or everything left waits on the user.

def run_path(root):
    return p(root, ".daedalus/run.json")


AFTER = re.compile(r"^\s*(?:[-*]\s*)?\**After\**\s*:\**\s*(.+?)\s*$", re.M | re.I)


def plan_milestones(root, cfg):
    """The plan's milestones in plan order ('## M1 — name' headings), with their unticked steps and
    their ordering rule: an `After: M8` line (or `After: all` for the last ones)."""
    md = read(p(root, cfg["plan"] or ".daedalus/plan.md"))
    heads = list(re.finditer(r"^##\s+(M\d+[a-z]?)\b[\s—–:.-]*(.*)$", md, re.M))
    out = []
    for i, m in enumerate(heads):
        body = md[m.end():heads[i + 1].start() if i + 1 < len(heads) else len(md)]
        body = re.split(r"^##\s", body, maxsplit=1, flags=re.M)[0]
        rule = AFTER.search(body)
        after = rule.group(1).strip().rstrip(".") if rule else ""
        out.append({"id": m.group(1), "title": m.group(2).strip(),
                    "open_steps": len(re.findall(r"^\s*[-*]\s*\[ \]", body, re.M)),
                    "after": "all" if re.match(r"(all|everything|the rest|last)\b", after, re.I) else
                    re.findall(r"\bM\d+[a-z]?\b", after)})
    return out


def build_order(ms):
    """Plan order, moved only as far as the `After:` rules need. `After: all` waits for every
    milestone that doesn't say it too. A cycle falls back to plan order."""
    ids = [m["id"] for m in ms]
    deps = {m["id"]: ([x["id"] for x in ms if x["after"] != "all"] if m["after"] == "all" else
                      [x for x in m["after"] if x in ids and x != m["id"]]) for m in ms}
    order, left = [], list(ms)
    while left:
        nxt = next((m for m in left if all(d in [o["id"] for o in order] for d in deps[m["id"]])), left[0])
        order.append(nxt)
        left.remove(nxt)
    return order, deps


def waits_on_user(root, cfg, m):
    """An open milestone whose steps are all ticked and whose only open requirements are manual checks."""
    if m["open_steps"]:
        return False
    specs, problems = load_specs(root)
    state = load_json(requirements_path(root), {})
    reqs = [r for s in specs if s.get("milestone") == m["id"] for r in s.get("requirements") or []]
    if problems or not reqs:
        return False
    open_reqs = [r for r in reqs if not state.get(r["id"], {}).get("passes")]
    e2e_ok = not cfg["requirements"].get("milestone_needs_e2e", True) or any(
        r.get("verify") == "e2e" and state.get(r["id"], {}).get("passes") for r in reqs)
    return bool(open_reqs) and e2e_ok and all(r.get("verify") == "manual" for r in open_reqs)


def run_report(root, cfg, details=None):
    """(all_done, lines): every milestone of the plan in build order, done or open, as the harness sees
    it, and which one is next. `details`, if given, collects which milestones only wait on the user."""
    lines, all_done = [], True
    ms = plan_milestones(root, cfg)
    order, deps = build_order(ms)
    built = {m["id"] for m in ms if not m["open_steps"]}
    nxt = next((m for m in order if m["open_steps"] and all(d in built for d in deps[m["id"]])), None)
    for m in order:
        done, why = milestone_status(root, cfg, m["id"])
        all_done = all_done and done
        name = f"{m['id']}" + (f" — {m['title']}" if m["title"] else "")
        rule = (" (after every other milestone)" if m["after"] == "all" else
                f" (after {', '.join(m['after'])})" if m["after"] else "")
        if done:
            lines.append(f"- {name}{rule}: DONE (gate met)")
        elif waits_on_user(root, cfg, m):
            lines.append(f"- {name}{rule}: built; waits only on the user's checks (`dd.py try {m['id']}`)")
            if details is not None:
                details.setdefault("waiting", []).append(m["id"])
        else:
            steps = f"{m['open_steps']} step(s) unticked in the plan, " if m["open_steps"] else ""
            lines.append(f"- {name}{rule}: gate open ({steps}{len(why)} reason(s); `dd.py milestone {m['id']}`)")
            if details is not None:
                details.setdefault("open", []).append(m["id"])
    if not ms:
        lines.append(f"- no milestones in {cfg['plan'] or '.daedalus/plan.md'} (headings like `## M1 — <name>`)")
        all_done = False
    if nxt:
        lines.append(f"Next in build order: {nxt['id']}.")
    changes = changes_text(root)
    if changes:
        lines.append("Changes from the user not in the plan yet (fold them in before the next step):\n" + changes)
    return all_done, lines


def run_start(root, cfg):
    run = load_json(run_path(root), {})
    if not run.get("active"):
        run = {"active": True, "started": dt.datetime.now().isoformat(timespec="seconds"),
               "head_at_start": git(root, "rev-parse", "--short", "HEAD")}
        save_json(run_path(root), run)
        lead = "Build-all run started"
    else:
        lead = f"Build-all run already active (since {run.get('started')})"
    _, lines = run_report(root, cfg)
    return (f"{lead}. Until it ends, the Stop hook sends the session back to work after each commit. End it with "
            f"`python \"{DD}\" run end --reason \"<why>\"` once every milestone is done or everything left waits "
            "on the user.\n" + "\n".join(lines))


OUTCOMES = {"complete": "complete", "waiting_on_user": "waiting on you", "stopped": "stopped", "stalled": "stalled"}


def run_end(root, cfg, reason, outcome=None):
    """End the run. Its outcome: complete; waiting on the user (everything left is their checks or the
    board's decisions); stopped (work is left); or stalled (set by the Stop hook)."""
    run = load_json(run_path(root), {})
    details = {}
    all_done, lines = run_report(root, cfg, details)
    if not outcome:
        outcome = ("complete" if all_done else
                   "waiting_on_user" if not details.get("open") and not open_changes(root) else "stopped")
    run.update(active=False, ended=dt.datetime.now().isoformat(timespec="seconds"), reason=reason, outcome=outcome)
    save_json(run_path(root), run)
    return run, lines


def run_text(root, cfg):
    run = load_json(run_path(root), {})
    _, lines = run_report(root, cfg)
    if run.get("active"):
        head = f"Build-all run active since {run.get('started')}."
    elif run.get("ended"):
        head = (f"No build-all run active. The last one ended {run['ended']} "
                f"({OUTCOMES.get(run.get('outcome'), run.get('outcome'))}): {run.get('reason')}")
    else:
        head = "No build-all run active."
    return head + "\n" + "\n".join(lines)


def run_nudge(root, cfg, data):
    """Stop hook, during a build-all run: send the session back to work, or let it stop. Returns the
    hook's answer or None."""
    run = load_json(run_path(root), {})
    if not run.get("active"):
        return None
    pct, _ = context_pct(cfg, data)
    if pct is not None and pct >= pause_at(cfg):
        return None  # the pause comes first; `start continue` picks the run up again
    head = git(root, "rev-parse", "HEAD")
    if run.get("nudged_at") == head:
        run_end(root, cfg, "the session tried to stop twice without committing anything new", outcome="stalled")
        return {"systemMessage": "Daedalus: the build-all run stopped because no new step was committed since the "
                                 f"last nudge. Read the last report; {cmd_name('build')} all picks it up again."}
    run.update(nudged_at=head, nudges=run.get("nudges", 0) + 1)
    save_json(run_path(root), run)
    _, lines = run_report(root, cfg)
    unsaved = unsaved_commits(root, cfg)
    save_note = (f" {len(unsaved)} commit(s) since the last handoff: run {cmd_name('save')} when a milestone closes."
                 if unsaved else "")
    return {"decision": "block", "reason":
            f"Daedalus: a build-all run is active (since {run.get('started')}), so don't stop yet. Carry on with the "
            f"next step that isn't blocked ({cmd_name('build')} all).{save_note}\nMilestones:\n" + "\n".join(lines) +
            f"\nIf every milestone is done, or everything left waits on the user (a decision, a manual check, a "
            f"step that stayed blocked), or the user asked you to stop: save, run `python \"{DD}\" run end --reason "
            f"\"<why>\"`, and give the final report."}


# --- try it yourself ------------------------------------------------------------------------
#
# A requirement only the user can judge has "verify": "manual" and, for the walkthrough, a "try":
#   {"id": "UI-3", "text": "...", "verify": "manual",
#    "try": {"title": "Sign-up works on a phone", "steps": ["Run `npm run dev`", "Open …/signup"],
#            "expect": "You land on the dashboard and see your name"}}
# Any requirement may carry "try" (then it's shown as "see it yourself"); manual ones must be
# recorded by the user: `/daedalus:verify UI-3 ok` typed in Claude Code (the hook records it; the
# model can't invoke that command), or `dd.py try --ask` in the user's own terminal.

OK_WORDS = ("ok", "pass", "passed", "yes", "y", "works", "worked", "good", "verified", "done", "✓", "✅")
FAIL_WORDS = ("fail", "failed", "no", "n", "broken", "bug", "wrong", "doesn't", "doesnt", "✗", "❌")


def record_manual(root, rid, ok, note, by):
    state = load_json(requirements_path(root), {})
    state[rid] = {**state.get(rid, {}), "verify": "manual", "passes": ok, "evidence": [note] if note else [],
                  "at": dt.datetime.now().isoformat(timespec="seconds"), "by": by}
    save_json(requirements_path(root), state)


def find_requirement(root, rid):
    specs, _ = load_specs(root)
    return next(((s, r) for s in specs for r in s.get("requirements") or []
                 if r.get("id", "").lower() == rid.lower()), (None, None))


def parse_verdict(args):
    """'UI-3 ok looks right' → ('UI-3', True, 'looks right'); no clear ok/fail word → verdict None."""
    words = (args or "").replace(",", " ").split()
    if not words:
        return None, None, ""
    rid = words[0].strip(":;.")
    rest = words[1:]
    if rest and rest[0].lower().strip(":;.!") in OK_WORDS:
        return rid, True, " ".join(rest[1:])
    if rest and rest[0].lower().strip(":;.!") in FAIL_WORDS:
        return rid, False, " ".join(rest[1:])
    return rid, None, " ".join(rest)


def try_items(root, cfg, which=None):
    """(spec, requirement, state) for the requirements a person can try, in milestone order."""
    specs, _ = load_specs(root)
    state = load_json(requirements_path(root), {})
    out = []
    for s in specs:
        if not active(s):
            continue
        for r in s.get("requirements") or []:
            if which and which.upper() not in (str(s.get("milestone", "")).upper(), r.get("id", "").upper()):
                continue
            if r.get("verify") == "manual" or r.get("try"):
                out.append((s, r, state.get(r.get("id", ""), {})))
    return sorted(out, key=lambda x: (str(x[0].get("milestone", "")), x[1].get("id", "")))


def try_text(root, cfg, which=None):
    items = try_items(root, cfg, which)
    if not items:
        return ("Nothing to try yet" + (f" for {which}" if which else "") + ": no requirement in .daedalus/specs/ "
                "is marked for a manual check (\"verify\": \"manual\") or has \"try\" steps.")
    waiting = [x for x in items if x[1].get("verify") == "manual" and not x[2].get("passes")]
    lines = [f"Try it yourself{' — ' + which if which else ''}: "
             + (f"{len(waiting)} check(s) waiting for you" if waiting else "nothing waiting for you"), ""]
    for i, (s, r, st) in enumerate(items, 1):
        t = r.get("try") or {}
        manual = r.get("verify") == "manual"
        lines.append(f"{i}. {r['id']} — {t.get('title') or r.get('text', '')}"
                     + ("" if manual else "  (optional: the gates already prove this one)"))
        steps = t.get("steps") or []
        if steps:
            lines.append("   Do:")
            lines += [f"     {n}. {step}" for n, step in enumerate(steps, 1)]
        elif manual:
            lines.append("   Do: (no steps written yet: ask the agent to add \"try\" steps to "
                         f"{s.get('_file', 'the spec')})")
        if t.get("expect"):
            lines.append(f"   You should see: {t['expect']}")
        if manual:
            if st.get("passes"):
                status = f"verified by you ({st.get('at', '')[:10]})"
            elif st.get("by") and st.get("at"):
                status = f"you reported a problem ({st.get('at', '')[:10]}): {'; '.join(st.get('evidence') or [])}"
            else:
                status = "waiting for you"
            lines.append(f"   Status: {status}")
        lines.append("")
    if waiting:
        lines += ["Tell the harness what you saw (only you can record these):",
                  f"  in Claude Code:  /daedalus:verify {waiting[0][1]['id']} ok   or   "
                  f"/daedalus:verify {waiting[0][1]['id']} fail <what happened>",
                  f"  in Codex:        $daedalus:verify {waiting[0][1]['id']} ok   (or fail <what happened>)",
                  f"  in a terminal:   python \"{DD}\" try --ask   (walks you through each check)"]
    return "\n".join(lines)


def verify_from_user(root, cfg, args, by):
    """Record a check the user typed (`/daedalus:verify UI-3 ok …`). Returns the note for the agent."""
    rid, ok, note = parse_verdict(args)
    usage = f"`{cmd_name('verify')} <ID> ok` or `{cmd_name('verify')} <ID> fail <what happened>`"
    if not rid:
        return f"Daedalus: the user ran {cmd_name('verify')} without an ID. Show them {cmd_name('try')} and the form {usage}."
    s, r = find_requirement(root, rid)
    group = rid.lower() == "all" or bool(re.fullmatch(r"M\d+[a-z]?", rid, re.I))
    if not r and group:  # `all ok`, `M3 ok`: every check still waiting (in that milestone)
        waiting = [x[1]["id"] for x in try_items(root, cfg, None if rid.lower() == "all" else rid)
                   if x[1].get("verify") == "manual" and not x[2].get("by")]
        if ok is not True:
            return (f"Daedalus: nothing recorded: `{rid} fail` doesn't say which check failed. Ask the user which one, "
                    f"as `{cmd_name('verify')} <ID> fail <what happened>`. Waiting: {', '.join(waiting) or 'none'}.")
        if not waiting:
            return f"Daedalus: no checks are waiting for the user{'' if rid.lower() == 'all' else ' in ' + rid}."
        for x in waiting:
            record_manual(root, x, True, note or f"verified with `{rid} ok`", by)
        return (f"Daedalus recorded {', '.join(waiting)} as verified by the user (they typed `{rid} ok`). Tell them, "
                "in one line, exactly which checks that covered, and carry on.")
    if not r:
        ids = [x[1]["id"] for x in try_items(root, cfg) if x[1].get("verify") == "manual"]
        return (f"Daedalus: there's no requirement {rid}. Checks waiting for the user: {', '.join(ids) or 'none'}. "
                f"Ask them to retype: {usage}.")
    if r.get("verify") != "manual":
        return (f"Daedalus: {r['id']} is proven by the gates ({r.get('verify', 'test')}), so nothing was recorded. "
                + ("The user reports it doesn't work: treat that as a bug the tests miss: add a failing test for it "
                   "through the test writer, then fix it." if ok is False else "Tell them it's already covered."))
    if ok is None:
        return (f"Daedalus: nothing recorded for {r['id']}: the command needs ok or fail. Ask the user to retype: "
                f"{usage}.")
    record_manual(root, r["id"], ok, note or ("worked as described" if ok else ""), by)
    if ok:
        return (f"Daedalus recorded {r['id']} as verified by the user"
                + (f" ({note})" if note else "") + ". Acknowledge it in one line and carry on.")
    return (f"Daedalus recorded {r['id']} as NOT working, reported by the user: {note or '(no details)'}. Thank them, "
            "add it to the board, and fix it through the build loop (spec, tests, builder, review). Ask what "
            "happened if the report isn't clear enough to act on.")


def try_ask(root, cfg, which=None):
    """Interactive walkthrough in the user's own terminal."""
    if not sys.stdin.isatty():
        sys.exit("`try --ask` asks you about each check, so run it yourself in a terminal (not through an agent).")
    items = [x for x in try_items(root, cfg, which) if x[1].get("verify") == "manual"]
    if not items:
        print("No manual checks" + (f" for {which}" if which else "") + ".")
        return
    for s, r, st in items:
        t = r.get("try") or {}
        print(f"\n{r['id']} — {t.get('title') or r.get('text', '')}")
        for n, step in enumerate(t.get("steps") or [], 1):
            print(f"  {n}. {step}")
        if t.get("expect"):
            print(f"  You should see: {t['expect']}")
        if st.get("passes"):
            print("  (already verified; Enter to keep it)")
        ans = input("  Did it work? [y]es / [n]o / Enter to skip: ").strip().lower()
        if ans in ("y", "yes"):
            note = input("  Anything to note? (Enter for none): ").strip()
            record_manual(root, r["id"], True, note or "worked as described", "user (terminal)")
            print("  Recorded: verified.")
        elif ans in ("n", "no"):
            note = ""
            while not note:
                note = input("  What happened instead? ").strip()
            record_manual(root, r["id"], False, note, "user (terminal)")
            print("  Recorded: not working. The agent will see it at the next start.")


# --- what the user typed --------------------------------------------------------------------
#
# `/daedalus:verify` and `/daedalus:change` are the user's own words: a check only they can make,
# and a change to what gets built. Claude Code's UserPromptExpansion hook sees them when the
# session is idle. A message sent while the session is busy is queued and folded into the running
# turn without any hook, so the hooks also read the transcript for queued messages Claude Code
# marks as typed by a person, and record each one once. Nobody may write the transcripts.

TYPED = re.compile(r"(?:^|(?<=\s))[/$]daedalus:(verify|change)\b", re.I)
VERDICT_LINE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]*-\d+[:.,]?\s+\S")


def typed_items(text):
    """[(command, args)] for each /daedalus:verify or /daedalus:change in a message. Several checks in
    one message are several records, whether each line repeats the command or just says `UI-4 ok`."""
    out = []
    marks = list(TYPED.finditer(text or ""))
    for k, m in enumerate(marks):
        body = text[m.end():marks[k + 1].start() if k + 1 < len(marks) else len(text)].strip()
        if m.group(1).lower() == "change":
            out.append(("change", body))
            continue
        current = []
        for line in (x.strip() for x in body.splitlines()):
            if current and VERDICT_LINE.match(line) and parse_verdict(line)[1] is not None:
                out.append(("verify", " ".join(current)))
                current = [line]
            elif line:
                current.append(line)
        out.append(("verify", " ".join(current)))
    return out


def record_typed(root, cfg, text, by):
    """Record every verify and change in a message the user typed. Returns the notes for the agent."""
    notes = []
    for command, args in typed_items(text):
        if command == "verify":
            notes.append(verify_from_user(root, cfg, args, by))
        else:
            notes.append(change_from_user(root, args, by))
    return notes


def typed_path(root):
    return p(root, ".daedalus/typed.json")


def queued_typed(root, cfg, data):
    """Daedalus commands the user sent while the session was busy (Claude Code queues them and folds
    them into the running turn, and no hook fires): recorded once each, from the transcript entries
    Claude Code writes for a person's queued message. Returns the notes, or []."""
    tp = data.get("transcript_path") or ""
    if HOST["name"] != "claude" or not tp or "/subagents/" in tp.replace("\\", "/"):
        return []
    lines = transcript_tail(tp)
    if not lines or not any('"queued_command"' in ln and "daedalus:" in ln for ln in lines):
        return []
    seen_state = load_json(typed_path(root), {})
    seen = set(seen_state.get("seen") or [])
    notes, new = [], []
    for ln in lines:
        if '"queued_command"' not in ln or "daedalus:" not in ln:
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        att = d.get("attachment") if isinstance(d, dict) else None
        if d.get("type") != "attachment" or not isinstance(att, dict) or att.get("type") != "queued_command":
            continue
        if (att.get("origin") or {}).get("kind") != "human":
            continue
        key = att.get("source_uuid") or d.get("uuid") or att.get("prompt", "")
        if key in seen:
            continue
        seen.add(key)
        new.append(key)
        notes += [n.replace("Daedalus recorded", "Daedalus recorded (sent while you were working)", 1)
                  for n in record_typed(root, cfg, att.get("prompt") or "", "user (sent while the session was busy)")]
    if new:
        save_json(typed_path(root), {"seen": (seen_state.get("seen") or [])[-400:] + new})
    return notes


# --- changes to what gets built -----------------------------------------------------------
#
# `/daedalus:change <what you want>`, typed by the user, goes into .daedalus/changes.json. Until the
# orchestrator has put it in the plan (`dd.py change done <n> --where "M9"`), session start and the
# build-all nudge show it, so a running build folds it in at the next step.

def changes_path(root):
    return p(root, ".daedalus/changes.json")


def change_from_user(root, text, by):
    text = (text or "").strip()
    if not text:
        return (f"Daedalus: the user ran {cmd_name('change')} without saying what to change. Ask them to retype it: "
                f"`{cmd_name('change')} <what you want>`.")
    items = load_json(changes_path(root), [])
    if any(c.get("text") == text for c in items):
        return f"Daedalus: that change is already recorded: {text[:80]}"
    n = max([c.get("n", 0) for c in items] + [0]) + 1
    items.append({"n": n, "text": text, "at": dt.datetime.now().isoformat(timespec="minutes"), "by": by,
                  "state": "open"})
    save_json(changes_path(root), items)
    return (f"Daedalus recorded change {n} from the user: \"{text}\". Fold it into the plan now (a new milestone "
            "or steps in one, with its spec; an ordering like \"build this last\" becomes an `After:` line on the "
            f"milestone), then mark it with `python \"{DD}\" change done {n} --where \"<milestone>\"`. If it "
            "contradicts something already built or decided, ask the user one short question first.")


def open_changes(root):
    return [c for c in load_json(changes_path(root), []) if c.get("state") == "open"]


def changes_text(root):
    items = open_changes(root)
    if not items:
        return ""
    return "\n".join(f"- change {c['n']} ({c['at'].replace('T', ' ')}): {c['text']}" for c in items)


def requirements_summary(root, cfg):
    specs, problems = load_specs(root)
    if not specs and not problems:
        return ""
    state = load_json(requirements_path(root), {})
    total = [r["id"] for s in specs if active(s) for r in s.get("requirements") or []]
    failing = [rid for rid in total if not state.get(rid, {}).get("passes")]
    ms = sorted({s.get("milestone") for s in specs if s.get("milestone")})
    lines = [f"- {len(total) - len(failing)}/{len(total)} active requirements pass (decided by the gates)"]
    if failing:
        lines.append("- not passing: " + ", ".join(failing[:12]) + (" …" if len(failing) > 12 else ""))
    for m in ms:
        done, why = milestone_status(root, cfg, m)
        lines.append(f"- {m}: " + ("DONE (gate met)" if done else f"gate open ({len(why)} reason(s))"))
    waiting = [r["id"] for s in specs if active(s) for r in s.get("requirements") or []
               if r.get("verify") == "manual" and not state.get(r["id"], {}).get("passes")]
    if waiting:
        lines.append(f"- waiting for the user to try: {', '.join(waiting[:8])} (show them with {cmd_name('try')})")
    lines += [f"- spec problem: {x}" for x in problems[:5]]
    return "\n".join(lines)


# --- context budget -------------------------------------------------------------------------

TAILS = {}


def transcript_tail(path, tail_bytes=3_000_000):
    """The last lines of a transcript (read once per hook call), or None."""
    if (path, tail_bytes) not in TAILS:
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - tail_bytes))
                TAILS[(path, tail_bytes)] = f.read().decode("utf-8", "replace").splitlines()
        except (OSError, TypeError):
            TAILS[(path, tail_bytes)] = None
    return TAILS[(path, tail_bytes)]


def context_tokens(transcript_path, tail_bytes=3_000_000, sidechain=False):
    """(tokens, model) for the context right now: the latest model call's input (fresh + cache reads
    + cache writes), the same sum the status line's used_percentage uses. Main-thread calls only,
    unless `sidechain` (a subagent's own transcript). Reads only the end of the transcript."""
    lines = transcript_tail(transcript_path, tail_bytes)
    if lines is None:
        return None, None
    for line in reversed(lines):
        if '"usage"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") != "assistant" or (d.get("isSidechain") and not sidechain):
            continue
        msg = d.get("message") or {}
        u = msg.get("usage") or {}
        if "input_tokens" in u:
            return (sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                                "cache_creation_input_tokens")), msg.get("model") or "")
    return None, None


# Claude Code's documented defaults (code.claude.com/docs/en/model-config): these models run with a
# 1M window without the [1m] suffix; older ones only with it.
NATIVE_1M = re.compile(r"claude-(fable-\d|sonnet-([5-9]|\d\d)|haiku-(5-[5-9]|[6-9]|\d\d)|opus-(4-([7-9]|\d\d)|[5-9]|\d\d))", re.I)


def context_window(cfg, tokens, model=""):
    """The model's context window, in Claude Code's order: explicit setting, the env caps Claude Code
    itself honours, the model's documented default, then usage (over 200k can only mean 1M)."""
    w = os.environ.get("DAEDALUS_CONTEXT_WINDOW") or cfg["context"].get("window")
    if isinstance(w, (int, float)) or (isinstance(w, str) and w.isdigit()):
        return int(w)
    if os.environ.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "").isdigit():
        return int(os.environ["CLAUDE_CODE_MAX_CONTEXT_TOKENS"])
    if os.environ.get("CLAUDE_CODE_DISABLE_1M_CONTEXT") == "1":
        return 200_000
    if "[1m]" in model.lower() or NATIVE_1M.search(model) or (tokens or 0) > 200_000:
        return 1_000_000
    return 200_000


def subagent_transcript(data):
    """A subagent's own transcript: given directly, or <session>/subagents/agent-<id>.jsonl."""
    if data.get("agent_transcript_path"):
        return data["agent_transcript_path"]
    tp = data.get("transcript_path") or ""
    if "/subagents/" in tp.replace("\\", "/"):
        return tp
    cand = os.path.join(os.path.splitext(tp)[0], "subagents", f"agent-{data.get('agent_id')}.jsonl")
    return cand if tp and os.path.isfile(cand) else None


def codex_context(transcript_path, tail_bytes=3_000_000):
    """(tokens, window) from a Codex rollout: the latest token_count event's last_token_usage (the
    context of the latest model call, as Codex's own status shows it) against model_context_window.
    In a Codex subagent the hook's transcript_path is already the agent's own rollout."""
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - tail_bytes))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError):
        return None, None
    for line in reversed(lines):
        if '"token_count"' not in line:
            continue
        try:
            info = (json.loads(line).get("payload") or {}).get("info") or {}
        except ValueError:
            continue
        last = info.get("last_token_usage") or {}
        if last.get("total_tokens") and info.get("model_context_window"):
            return last["total_tokens"], info["model_context_window"]
    return None, None


def context_pct(cfg, data):
    """Context use of whoever is acting: the main session, or a subagent's own window."""
    if HOST["name"] == "codex":
        tokens, window = codex_context(data.get("transcript_path"))
        if tokens is None:
            return None, None
        w = os.environ.get("DAEDALUS_CONTEXT_WINDOW") or cfg["context"].get("window")
        window = int(w) if isinstance(w, (int, float)) or (isinstance(w, str) and w.isdigit()) else window
        return round(100 * tokens / window, 1), tokens
    if data.get("agent_id"):
        path = subagent_transcript(data)
        tokens, model = context_tokens(path, sidechain=True) if path else (None, None)
    else:
        tokens, model = context_tokens(data.get("transcript_path"))
    if tokens is None:
        return None, None
    return round(100 * tokens / context_window(cfg, tokens, model), 1), tokens


def ctx_state_path(root):
    return p(root, ".daedalus/context.json")


def ctx_state(root, session):
    st = load_json(ctx_state_path(root), {})
    return st if st.get("session_id") == session else {"session_id": session}


def compacting(cfg):
    return cfg["context"].get("on_limit", "compact") == "compact"


def pause_at(cfg):
    """Where the hard pause starts: the limit itself, or, when the host compacts at the limit, a
    margin above it, reached only if compaction didn't happen."""
    c = cfg["context"]
    if c.get("pause_pct"):
        return c["pause_pct"]
    return min(c["limit_pct"] + 10, 95) if compacting(cfg) else c["limit_pct"]


def pause_done(root, cfg, st):
    """The pause is complete once a real handoff was written after the limit was hit."""
    h = newest_handoff(root, cfg, real_only=True)
    return bool(h and st.get("limit_hit_at") and os.path.getmtime(h) > st["limit_hit_at"])


SAFE_SEGMENT = re.compile(r"^\s*(cd\s|git\s+(-C\s+\S+\s+)?(status|diff|log|add|commit|show|rev-parse|branch|stash)\b|"
                          r"python3?\s+\S*dd\.py\b|echo\b|date\b|ls\b|pwd\b)", re.I)


def allowed_while_paused(root, cfg, tool, ti):
    """While over the limit, only the save itself may run: bookkeeping files, git, the CLI."""
    if tool in ("Skill", "TodoWrite"):
        return True
    if tool in ("Read", "Edit", "Write", "MultiEdit"):
        rel = rel_to_root(root, ti.get("file_path") or "")
        return bool(rel and bookkeeping(cfg, rel))
    if tool in ("Bash", "PowerShell"):
        segments = [s for s in re.split(r"&&|\|\||;|\|", ti.get("command") or "") if s.strip()]
        return bool(segments) and all(SAFE_SEGMENT.match(s) for s in segments)
    return False


def context_check(root, cfg, data, items=None):
    """Returns ('deny', reason), ('note', text) or (None, None)."""
    items = items if items is not None else [data]
    pct, tokens = context_pct(cfg, data)
    if pct is None:
        return None, None
    c = cfg["context"]
    hard = pause_at(cfg)
    session = data.get("session_id", "")
    if role_of(data) != "main":  # a subagent's own window: wrap up and report back, nothing to save
        if pct >= hard:
            return "deny", (f"Daedalus: your context is at {pct}%, over the {hard}% limit. Stop now and "
                            "report back to the orchestrator: what's done, what's left (files, state), and "
                            "how to verify it. Don't start anything else.")
        if pct >= c["warn_pct"]:
            return "note", (f"Daedalus: your context is at {pct}% (limit {c['limit_pct']}%). Finish what you're "
                            "doing and report back; don't take on more.")
        return None, None
    if pct >= hard:
        st = ctx_state(root, session)
        if not st.get("limit_hit_at"):
            st.update(limit_hit_at=time.time(), pct=pct)
            save_json(ctx_state_path(root), st)
        if all(allowed_while_paused(root, cfg, d.get("tool_name", ""), d.get("tool_input") or {}) for d in items):
            return None, None
        why = (f"the context is at {pct}% ({tokens:,} tokens), over the {hard}% limit" if not compacting(cfg) else
               f"the context is at {pct}% ({tokens:,} tokens): it should have compacted itself at {c['limit_pct']}%, "
               "but it didn't (auto-compaction off, or the host's compaction setting missing: `dd.py doctor` checks)")
        return "deny", (f"Daedalus: {why}. Stop the work now and pause: run {cmd_name('pause')} (gates, commit "
                        "if green, status, handoff, board). Until the handoff is written, only saving is allowed. "
                        f"Then tell the user to {clear_hint()} and then `{cmd_name('start')} continue`.")
    if pct >= c["warn_pct"]:
        st = ctx_state(root, session)
        if pct >= st.get("warned_pct", 0) + 3:  # don't repeat the note on every call
            st["warned_pct"] = pct
            save_json(ctx_state_path(root), st)
            if compacting(cfg):
                return "note", (f"Daedalus: the context is at {pct}%. At {c['limit_pct']}% it compacts itself and "
                                "you carry on from the saved state, so make that state complete now: finish the "
                                "current step (don't start a new one), commit it if the gates pass, and write a "
                                f"handoff with {cmd_name('save')}. Then continue with the next step.")
            return "note", (f"Daedalus: the context is at {pct}% (limit {c['limit_pct']}%). Finish the current "
                            f"step, don't start a new one, and run {cmd_name('pause')} before the limit.")
    return None, None


# --- roles ----------------------------------------------------------------------------------

ROLES = ("builder", "test-writer", "reviewer", "researcher")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def role_of(data):
    """'main' for the main session, the Daedalus role for our plugin agents (agent_type
    'daedalus:<role>'; agent names can't contain ':', so nothing else can claim it), else 'other'."""
    at = data.get("agent_type") or ""
    if not data.get("agent_id") and not at:
        return "main"
    # Claude Code: plugin agents are 'daedalus:<role>'. Codex: agent roles from .codex/agents/ are
    # 'daedalus-<role>' (Codex role names allow letters, digits, '-' and '_').
    prefix = "daedalus:" if HOST["name"] == "claude" else "daedalus-"
    if at.startswith(prefix) and at[len(prefix):] in ROLES:
        return at[len(prefix):]
    return "other"


def tool_path(root, ti):
    path = ti.get("file_path") or ti.get("notebook_path") or ti.get("path") or ""
    return rel_to_root(root, path) if path else None


def prefixes(globs):
    """Literal leading folders of globs ('src/**' → 'src/'), for spotting paths in shell commands."""
    out = []
    for g in globs:
        lit = re.split(r"[*?\[]", g, 1)[0]
        if "/" in lit and lit.strip("/"):
            out.append(lit[: lit.rfind("/") + 1].lstrip("./"))
    return out


def transcripts_dir(data):
    """Where the host keeps this session's transcripts: the harness reads typed commands from them."""
    tp = data.get("transcript_path") or ""
    return os.path.dirname(os.path.abspath(tp)) if tp else None


def is_transcript(tdir, path):
    """A session transcript (.jsonl) in the host's transcript folder, or that folder itself."""
    ap = os.path.abspath(path)
    return ap == tdir or (ap.startswith(tdir + os.sep) and (ap.endswith(".jsonl") or "." not in os.path.basename(ap)))


def integrity_check(root, cfg, data):
    """Rules for everyone, the orchestrator included: the gates and their results are the harness's.
    Returns ("deny" | "ask", reason) or (None, None)."""
    role = role_of(data)
    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}
    rel = tool_path(root, ti) if tool in EDIT_TOOLS else None
    eff = effects_of(root, data)
    writes = eff["writes"] if eff else []
    if (eff and (runs_dd(eff, "verify-manual") or runs_dd(eff, "try", "--ask"))) or (
            tool == "Skill" and re.search(r"(^|:)verify$", str(ti.get("skill") or ti.get("name") or ""))):
        return "deny", ("Daedalus: manual checks are the user's call. Show them the steps "
                        f"(`python <dd.py> try`) and ask them to record what they saw: `{cmd_name('verify')} <ID> ok` "
                        "or `fail <what happened>`, or `python <dd.py> try --ask` in their own terminal.")
    if (eff and runs_dd(eff, "change", "add")) or (
            tool == "Skill" and re.search(r"(^|:)change$", str(ti.get("skill") or ti.get("name") or ""))):
        return "deny", ("Daedalus: what gets built is the user's call. If you think the plan needs a change, ask them; "
                        f"they record it with `{cmd_name('change')} <what they want>`.")
    if eff and nested_agent(eff):
        return "deny", ("Daedalus: agents don't start Claude Code or Codex sessions from the shell: a nested session "
                        "would run outside its role. The orchestrator starts agents "
                        + ("with the Agent tool." if HOST["name"] == "claude" else "with spawn_agent (daedalus-<role>)."))
    tdir = transcripts_dir(data)
    if tdir:
        target = ti.get("file_path") or ti.get("notebook_path") or ""
        if any(is_transcript(tdir, x) for x in ([target] if tool in EDIT_TOOLS and target else []) + writes):
            return "deny", ("Daedalus: the session transcripts are the host's record of what the user typed; "
                            "nobody writes them.")
    if (rel and rel.startswith(".daedalus/gates/")) or under(root, writes, [".daedalus/gates/**"]):
        return "deny", ("Daedalus: .daedalus/gates/ (baseline, requirement results, run logs) is written only by "
                        "the gates. Run `dd.py gate` instead.")
    if (rel == ".daedalus/config.json") or under(root, writes, [".daedalus/config.json"]):
        if role == "main":
            return "ask", "Daedalus: changing the harness config (gates, thresholds, roles) needs the user's OK."
        return "deny", "Daedalus: agents don't change the harness config. Report what you'd change and why."
    # The hosts' own settings decide whether the hooks, the agents and compaction run at all.
    host_files = [".codex/**", ".claude/settings.json", ".claude/settings.local.json"]
    if (rel and matches(rel, host_files)) or under(root, writes, host_files):
        if role == "main":
            return "ask", ("Daedalus: this changes the agent host's settings (hooks, agents, compaction), which needs "
                           "the user's OK. `dd.py setup` writes the ones Daedalus needs.")
        return "deny", "Daedalus: agents don't change the agent host's settings. Report what you'd change and why."
    return None, None


def role_check(root, cfg, data):
    """Per-role permissions. Returns a refusal reason or None."""
    role = role_of(data)
    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}
    src = cfg["roles"]["source"]
    tests = cfg["roles"]["tests"] + cfg["roles"].get("test_config", []) + gate_files(root, cfg)
    rel = tool_path(root, ti) if tool in EDIT_TOOLS + ("Read", "Grep") else None
    is_src = bool(rel and matches(rel, src) and not matches(rel, tests))
    is_test = bool(rel and matches(rel, tests))
    eff = effects_of(root, data)
    writes = eff["writes"] if eff else []

    # The verdict file and its recorder belong to the reviewer alone.
    if role != "reviewer":
        if tool in EDIT_TOOLS and rel and rel.startswith(".daedalus/reviews/"):
            return f"Daedalus: only the reviewer agent writes reviews. Spawn {agent_name('reviewer')}."
        if eff and (runs_dd(eff, "review-record") or under(root, writes, [".daedalus/reviews/**"])):
            return f"Daedalus: only the reviewer agent records reviews. Spawn {agent_name('reviewer')}."

    if role == "main":
        if tool == "Agent" or tool == "Task":
            return None
        if cfg["mode"] == "orchestrated" and tool in EDIT_TOOLS and (is_src or is_test):
            kind = "tests" if is_test else "source"
            agent = agent_name("test-writer" if is_test else "builder")
            return (f"Daedalus (orchestrated mode): the orchestrator doesn't edit {kind} ({rel}). "
                    f"Spawn {agent} with the step, the spec and what it needs to know.")
        return None

    # Every subagent: no nested agents, no commits or pushes (the orchestrator commits after review).
    if tool in ("Agent", "Task"):
        return "Daedalus: subagents don't start other agents. Report back; the orchestrator decides who works next."
    if eff and any(g["sub"] in ("commit", "push") for g in eff["git"]):
        return "Daedalus: only the orchestrator commits, after the review. Report back instead."
    if eff and (runs_dd(eff, "run", "start") or runs_dd(eff, "run", "end") or runs_dd(eff, "change", "done")
                or runs_dd(eff, "deploy")):
        return ("Daedalus: only the orchestrator starts or ends a build-all run, deploys, or marks a change as "
                "planned. Report back instead.")

    if role == "builder":
        if tool in EDIT_TOOLS and is_test:
            return (f"Daedalus: the builder doesn't edit tests or test config ({rel}). If a test is wrong or the "
                    "spec can't be met, stop and report BLOCKED with the reason; the orchestrator decides.")
        hits = under(root, writes, tests)
        if hits:
            return (f"Daedalus: the builder doesn't change tests or test config from the shell either "
                    f"({', '.join(hits[:5])}). Report BLOCKED with the reason instead.")
        return None
    if role == "test-writer":
        if tool in EDIT_TOOLS and rel and not is_test:
            return f"Daedalus: the test writer writes only test files ({', '.join(tests)}); {rel} isn't one."
        blind = (f"Daedalus: the test writer works from the spec and the interface, not the implementation "
                 f"({rel or 'the whole project'}). Ask the orchestrator for any interface detail you need.")
        if tool == "Read" and is_src:
            return blind
        if tool == "Grep":  # a search over a folder that contains source would show the implementation
            target = (rel or "").rstrip("/")
            if is_src or not target or target == "." or any(
                    pre.startswith(target + "/") or target.startswith(pre.rstrip("/")) for pre in prefixes(src)):
                return blind + " Point Grep at tests/ or the docs instead."
        if eff:
            seen = [x for x in under(root, eff["reads"], src) if not matches(x, tests)]
            changed = [x for x in under(root, writes, src) if not matches(x, tests)]
            if changed:
                return (f"Daedalus: the test writer doesn't change implementation files from the shell either "
                        f"({', '.join(changed[:5])}).")
            if seen:
                return (f"Daedalus: the test writer doesn't read implementation files from the shell either "
                        f"({', '.join(seen[:5])}). Running the tests is fine; ask the orchestrator for interface details.")
        return None
    if role == "reviewer":
        if tool in EDIT_TOOLS and rel is not None and rel != REVIEW_DRAFT:
            return f"Daedalus: the reviewer reports, it doesn't fix. Write only {REVIEW_DRAFT} in the project."
        inside = in_project(root, writes, [REVIEW_DRAFT]) if eff else []
        if inside:
            return (f"Daedalus: the reviewer doesn't change project files from the shell ({', '.join(inside[:5])}). "
                    "Scratch files outside the project (/tmp, $TMPDIR, the scratchpad) are fine.")
        return None
    if role == "researcher":
        if tool in EDIT_TOOLS and rel is not None and not rel.startswith(".daedalus/research/"):
            return "Daedalus: the researcher writes only notes under .daedalus/research/ in the project."
        inside = in_project(root, writes, [".daedalus/research/**"]) if eff else []
        if inside:
            return (f"Daedalus: the researcher doesn't change project files from the shell ({', '.join(inside[:5])}). "
                    "Notes go under .daedalus/research/; scratch files outside the project are fine.")
        return None
    # Other subagents (Explore, general-purpose, ...) in orchestrated mode: no source or test edits.
    if cfg["mode"] == "orchestrated" and tool in EDIT_TOOLS and (is_src or is_test):
        return (f"Daedalus (orchestrated mode): only {agent_name('builder')} edits source and only {agent_name('test-writer')} "
                f"edits tests ({rel}).")
    return None


def agent_note(root, cfg, role):
    """Context injected when one of our agents starts: where things are, and its own rules."""
    rules = {
        "builder": "You may edit source but not tests; you never commit; the orchestrator commits after review.",
        "test-writer": f"You may write only test files ({', '.join(cfg['roles']['tests'])}) and may not read "
                       f"implementation files ({', '.join(cfg['roles']['source'])}).",
        "reviewer": f"You may write only {REVIEW_DRAFT}; record it with `review-record`.",
        "researcher": "You may write only notes under .daedalus/research/.",
    }[role]
    return (f"Daedalus project: {cfg['project'] or os.path.basename(root)} at {root}\n"
            f"Daedalus CLI: python \"{DD}\" (gate, review-record, status)\n"
            f"Your role: {role}. {rules} You can't start other agents. Context limit {cfg['context']['limit_pct']}% "
            "of your own window: past it, report back.")


# --- hooks ----------------------------------------------------------------------------------

HOST = {"name": "claude"}  # which agent runs the hook: claude (Claude Code) or codex (Codex CLI)


def cmd_name(skill):
    """How the user (or the agent) invokes one of our skills on this host."""
    return f"/daedalus:{skill}" if HOST["name"] == "claude" else f"$daedalus:{skill}"


def agent_name(role):
    """Our agent for a role, as this host names it."""
    return f"daedalus:{role}" if HOST["name"] == "claude" else f"daedalus-{role}"


def clear_hint():
    return "run /clear" if HOST["name"] == "claude" else "start a fresh session with /new"


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(0)


def emit_context(event, text):
    """Add text to the agent's context from a hook."""
    emit({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}})


HOOK_CTX = {}  # root + hook input of the running hook, for the refusal log


def log_refusal(reason):
    root, data = HOOK_CTX.get("root"), HOOK_CTX.get("data") or {}
    if not root:
        return
    try:
        with open(p(root, ".daedalus/hooks.log"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"), "tool": data.get("tool_name"),
                                "input": {k: str(v)[:2000] for k, v in (data.get("tool_input") or {}).items()},
                                "reason": reason[:300]}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def deny(reason):
    log_refusal(reason)
    emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                 "permissionDecisionReason": reason}})


def approvals_path(root):
    return p(root, ".daedalus/approvals.json")


def ask(reason, item=None):
    """Claude Code asks the user itself. Codex has no 'ask' answer for hooks (it would count as a failed
    hook and let the action through), so the action is refused, the agent asks in the chat, and the
    user's own reply, seen by the UserPromptSubmit hook that only a person's input fires, allows that
    exact action once."""
    if HOST["name"] == "claude":
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                     "permissionDecisionReason": reason}})
    root, data = HOOK_CTX.get("root"), item or HOOK_CTX.get("data") or {}
    ti = data.get("tool_input") or {}
    key = f"{data.get('tool_name')}:{ti.get('file_path') or ti.get('command') or ''}"[:400]
    session = (HOOK_CTX.get("data") or {}).get("session_id", "")
    st = load_json(approvals_path(root), {})
    g = st.get("granted") or {}
    if g.get("key") == key and g.get("session") == session:
        st.pop("granted")
        save_json(approvals_path(root), st)
        return  # approved once by the user's own reply
    st["pending"] = {"key": key, "reason": reason, "session": session, "at": time.time()}
    save_json(approvals_path(root), st)
    deny(reason + " Ask the user now, in one short question, and end your turn. If they reply yes, run "
                  "exactly the same action again: their reply allows it once.")


YES = re.compile(r"^\s*(yes|y|yep|ok|okay|sure|approve[d]?|go ahead|do it|da)\b", re.I)
NO = re.compile(r"^\s*(no|n|nope|don'?t|stop|cancel|nu)\b", re.I)


def user_reply_to_ask(root, data):
    """Codex: the user's answer to a pending question from ask(). Returns a note, or None."""
    st = load_json(approvals_path(root), {})
    pend = st.get("pending")
    if not pend or pend.get("session") != data.get("session_id", ""):
        return None
    prompt = data.get("prompt") or ""
    if YES.match(prompt):
        st["granted"] = {"key": pend["key"], "session": pend["session"], "at": time.time()}
        st.pop("pending")
        save_json(approvals_path(root), st)
        return f"Daedalus: the user approved ({pend['reason']}). Run that exact action again now; it's allowed once."
    if NO.match(prompt):
        st.pop("pending")
        save_json(approvals_path(root), st)
        return f"Daedalus: the user said no to: {pend['reason']} Don't retry it."
    return None


# --- what a shell command does --------------------------------------------------------------
#
# The guards judge what a Bash or PowerShell command writes, reads and runs, not which words it
# mentions. Matching the text refused about 100 harmless commands in one build: a `>` comparison
# in an inline script read as a redirect, "snapshot" read as a .snap file, `grep -n` read as
# `git commit -n`. So the command is split the way a shell splits it (quotes, heredocs, redirects,
# `cd`, simple variables), and code run inline (a heredoc fed to python or node, `-c`/`-e`
# arguments, a script file) counts as writing a path when it calls a write API and names the
# path in a string.

ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "for", "case", "esac", "!", "{",
            "}", "time", "function", "select", "in"}
PREFIX_CMDS = {"sudo", "env", "nice", "nohup", "command", "exec", "builtin", "stdbuf", "ionice", "timeout", "xargs",
               "doas", "caffeinate", "chronic"}
INTERPRETER = re.compile(r"^(python(\d+(\.\d+)?)?|py|pypy3?|node|nodejs|deno|bun|tsx|ts-node|perl|ruby|php)$", re.I)
SHELL_CMD = re.compile(r"^(bash|sh|zsh|dash|ksh|pwsh|powershell)$", re.I)
NULL_TARGET = re.compile(r"^(/dev/(null|stdout|stderr|tty|fd/\d+)|nul|\$null|NUL)$", re.I)
WRITERS_ALL = {"rm", "rmdir", "unlink", "shred", "truncate", "touch", "tee", "del", "erase", "rd", "ri",
               "remove-item", "clear-content", "new-item", "set-content", "add-content", "out-file", "mv", "move",
               "ren", "rename", "move-item", "mi"}
WRITERS_LAST = {"cp", "copy", "install", "ln", "rsync", "scp", "copy-item", "cpi"}
READERS = {"cat", "head", "tail", "less", "more", "nl", "bat", "tac", "od", "xxd", "hexdump", "strings", "view",
           "diff", "cmp", "get-content", "gc", "type", "tar", "zip", "awk", "gawk", "jq"}
GREPS = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "select-string", "sls"}
# Writing, reading and starting processes from inline code (Python, JavaScript, Ruby, Perl, PHP).
WRITE_API = re.compile(
    r"\bopen\(.{0,200}?,\s*(?:mode\s*=\s*)?['\"][rbt]*[wax]|\bmode\s*=\s*['\"][rbt]*[wax]|write_text|write_bytes|"
    r"writeFile|appendFile|createWriteStream|\b(?:rmSync|rmdirSync|unlinkSync|renameSync|copyFileSync|cpSync|"
    r"truncateSync|mkdirSync)\b|\bfs(?:\.promises)?\.(?:rm|rmdir|unlink|rename|copyFile|cp|truncate|mkdir)\(|"
    r"\bos\.(?:remove|unlink|rename|replace|rmdir|removedirs|makedirs|mkdir|truncate)\(|"
    r"\bshutil\.(?:copy\w*|move|rmtree)\(|\.unlink\(|\.touch\(|\.rename\(|\bFile\.(?:write|delete|open)|"
    r"\bfopen\(.{0,200}?['\"][wax]|\bfile_put_contents\(|\bunlink\(", re.S)
READ_API = re.compile(r"\bopen\(|read_text|read_bytes|readFile|createReadStream|readlines|\bfopen\(|"
                      r"\bfile_get_contents\(|\bFile\.read|\bfs\.read")
SPAWN_API = re.compile(r"\bsubprocess\.|\bos\.(?:system|popen|exec\w*|spawn\w*)\(|child_process|\bexecSync\b|"
                       r"\bspawnSync\b|\bexecFile\w*\(|\bspawn\(|\bexec\(|\bsystem\(|\bDeno\.(?:run|Command)|"
                       r"\bBun\.spawn|\bpty\.spawn")
STR_LIT = re.compile(r"""(['"`])((?:\\.|(?!\1)[^\\\n])*)\1""")
FILE_LIKE = re.compile(r"^\.?[\w@+-][\w@.+-]*\.[A-Za-z0-9]{1,10}$")


def shell_segments(text):
    """Split a command like a POSIX shell: (segments, substitutions). Each segment is
    {"words", "out", "in", "heredocs"}: its words with the quotes removed, its output and input
    redirect targets, and the heredoc bodies fed to it. Substitutions ($(…), `…`, <(…)) are the
    inner commands, which run too."""
    segs, subs = [], []
    seg = {"words": [], "out": [], "in": [], "heredocs": []}
    st = {"word": "", "has": False, "redirect": None}
    pending = []  # heredocs opened on the current line: (segment, delimiter, strip tabs)
    i, n = 0, len(text)

    def end_word():
        if st["has"]:
            if st["redirect"] == "out":
                seg["out"].append(st["word"])
            elif st["redirect"] == "in":
                seg["in"].append(st["word"])
            elif st["redirect"] is None:
                seg["words"].append(st["word"])
            st["redirect"] = None
        st["word"], st["has"] = "", False

    def end_seg():
        nonlocal seg
        end_word()
        st["redirect"] = None
        if seg["words"] or seg["out"] or seg["in"] or seg["heredocs"]:
            segs.append(seg)
        seg = {"words": [], "out": [], "in": [], "heredocs": []}

    def closing(j, open_ch, close_ch):
        """Index of the bracket that closes the one opened just before j (quotes respected)."""
        depth, q = 1, None
        while j < n:
            ch = text[j]
            if q:
                if ch == "\\" and q == '"':
                    j += 1
                elif ch == q:
                    q = None
            elif ch in "'\"":
                q = ch
            elif ch == "\\":
                j += 1
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return j
            j += 1
        return n

    def substitution(j):
        """text[j] starts `$(`, `<(`, `>(` or a backtick: record the inner command, return the end."""
        if text[j] == "`":
            k = text.find("`", j + 1)
            k = n if k < 0 else k
            subs.append(text[j + 1:k])
            return k + 1
        k = closing(j + 2, "(", ")")
        subs.append(text[j + 2:k])
        return k + 1

    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            if text[i + 1] == "\n":
                i += 2
                continue
            st["word"] += text[i + 1]
            st["has"] = True
            i += 2
            continue
        if ch == "'":
            k = text.find("'", i + 1)
            k = n if k < 0 else k
            st["word"] += text[i + 1:k]
            st["has"] = True
            i = k + 1
            continue
        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    st["word"] += text[j + 1] if text[j + 1] in '"\\$`' else text[j:j + 2]
                    j += 2
                    continue
                if text.startswith("$(", j) or text[j] == "`":
                    k = substitution(j)
                    st["word"] += text[j:k]
                    j = k
                    continue
                st["word"] += text[j]
                j += 1
            st["has"] = True
            i = j + 1
            continue
        if text.startswith("$(", i) or ch == "`" or (text.startswith("<(", i) or text.startswith(">(", i)):
            k = substitution(i)
            st["word"] += text[i:k]
            st["has"] = True
            i = k
            continue
        if ch == "#" and not st["has"]:
            k = text.find("\n", i)
            i = n if k < 0 else k
            continue
        if ch == "\n":
            end_seg()
            i += 1
            for owner, delim, strip in pending:  # heredoc bodies start on the next line
                lines = []
                while i < n:
                    k = text.find("\n", i)
                    k = n if k < 0 else k
                    line = text[i:k]
                    i = k + 1
                    if (line.lstrip("\t") if strip else line).rstrip("\r") == delim:
                        break
                    lines.append(line)
                owner["heredocs"].append("\n".join(lines))
            pending = []
            continue
        if ch in " \t\r":
            end_word()
            i += 1
            continue
        if text.startswith("<<<", i):
            end_word()
            st["redirect"] = "data"
            i += 3
            continue
        if text.startswith("<<", i):
            end_word()
            j = i + 2
            strip = j < n and text[j] == "-"
            j += 1 if strip else 0
            while j < n and text[j] in " \t":
                j += 1
            delim = ""
            while j < n and text[j] not in " \t\n;&|<>()":
                if text[j] in "'\"\\":
                    j += 1
                    continue
                delim += text[j]
                j += 1
            pending.append((seg, delim, strip))
            i = j
            continue
        if ch in "<>" or (ch == "&" and text.startswith("&>", i)):
            if st["word"].isdigit() and ch == ">":
                st["word"], st["has"] = "", False  # 2>file: the fd number isn't a word
            end_word()
            j = i + (2 if ch == "&" else 1)
            if ch == ">" and j < n and text[j] in ">|":
                j += 1
            if ch == ">" and j < n and text[j] == "&":  # >&2, 2>&1: a descriptor, not a file
                j += 1
                while j < n and (text[j].isdigit() or text[j] == "-"):
                    j += 1
                i = j
                continue
            st["redirect"] = "in" if ch == "<" else "out"
            i = j
            continue
        if ch in ";|&()":
            end_seg()
            i += 1
            continue
        st["word"] += ch
        st["has"] = True
        i += 1
    end_seg()
    return segs, subs


def expand(word, env):
    """$NAME, ${NAME}, $env:NAME and a leading ~, where the value is known."""
    def var(m):
        name = m.group(1) or m.group(2) or m.group(3)
        val = env.get(name, os.environ.get(name))
        if val is None and name in ("TMPDIR", "TEMP", "TMP"):
            val = tempfile_dir()
        return m.group(0) if val is None else val
    word = re.sub(r"\$env:([A-Za-z_]\w*)|\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)", var, word)
    if word.startswith("~"):
        word = os.path.expanduser(word)
    return word


def tempfile_dir():
    import tempfile
    return tempfile.gettempdir().replace("\\", "/")


def resolve(word, cwd):
    word = word.replace("\\", "/") if os.name == "nt" else word
    return os.path.normpath(word if os.path.isabs(word) else os.path.join(cwd, word)).replace("\\", "/")


def positionals(words, value_opts=()):
    """The non-option arguments (after `--`, everything is an argument)."""
    out, skip, rest = [], False, False
    for w in words:
        if skip:
            skip = False
            continue
        if rest or not w.startswith("-") or w == "-":
            out.append(w)
        elif w == "--":
            rest = True
        elif w in value_opts:
            skip = True
    return out


def agent_script(path, cwd):
    """A script file an agent wrote outside the project (a scratch file): its code is judged like
    inline code. The project's own scripts and the harness's are code under review, not probes."""
    root = find_root(cwd)
    plug = os.path.abspath(PLUGIN_ROOT).replace("\\", "/")
    ap = os.path.abspath(path).replace("\\", "/")
    if ap.startswith(plug + "/") or (root and rel_to_root(root, ap) is not None):
        return False
    if os.path.basename(ap) == "dd.py" and '"""Daedalus core' in read(ap, 300):  # another copy of the harness
        return False
    return os.path.isfile(ap) and os.path.getsize(ap) < 300_000


IMPORT_SPEC = re.compile(r"""\bfrom\s+(['"]).*?\1|\bimport\s*\(\s*(['"]).*?\2\s*\)|\brequire\(\s*(['"]).*?\3\s*\)|"""
                         r"""^\s*import\s+(['"]).*?\4""", re.M)


def path_literals(code):
    """Strings in source code that look like file paths (module imports aren't files it writes)."""
    for m in STR_LIT.finditer(IMPORT_SPEC.sub(" ", code)):
        s = m.group(2)
        if not s or s in (".", "..") or len(s) > 400 or re.search(r"\s", s) or "://" in s or s.startswith("-"):
            continue
        if "/" in s or FILE_LIKE.match(s):
            yield s


def shell_effects(cmd, cwd, powershell=False):
    """What a shell command does: {"writes", "reads"} (absolute paths, `cd` and simple variables
    followed), "runs" (each command's words), "code" (inline source it runs), "git" (git
    subcommands with their repo and arguments)."""
    eff = {"writes": [], "reads": [], "runs": [], "code": [], "git": []}
    if powershell:
        cmd = cmd.replace("\\", "/").replace("`", "'")  # PowerShell paths and its escape character
    _effects(cmd, cwd.replace("\\", "/"), {}, eff, {}, 0)
    return eff


def _code_effects(code, cwd, eff):
    eff["code"].append(code)
    paths = [resolve(x, cwd) for x in path_literals(code)]
    if WRITE_API.search(code):
        eff["writes"] += paths
    if READ_API.search(code):
        eff["reads"] += paths


def _effects(cmd, cwd, env, eff, scripts, depth):
    if depth > 4:
        return
    segs, subs = shell_segments(cmd)
    for sub in subs:
        _effects(sub, cwd, dict(env), eff, scripts, depth + 1)
    for seg in segs:
        raw = list(seg["words"])
        words = [expand(w, env) for w in raw]
        while words and ASSIGN.match(words[0]):
            name, val = ASSIGN.match(words[0]).groups()
            env[name] = os.path.join(tempfile_dir(), "mktemp") if "mktemp" in raw[0] else val
            words, raw = words[1:], raw[1:]
        while words and (words[0] in KEYWORDS or os.path.basename(words[0]) in PREFIX_CMDS):
            head = os.path.basename(words.pop(0))
            raw.pop(0)
            while words and (words[0].startswith("-") or ASSIGN.match(words[0]) or
                             (head == "timeout" and re.match(r"^\d+(\.\d+)?[smhd]?$", words[0]))):
                if head in ("sudo", "doas") and words[0] in ("-u", "-g", "-C"):
                    words, raw = words[1:], raw[1:]
                words, raw = words[1:], raw[1:]
        outs = [resolve(expand(t, env), cwd) for t in seg["out"] if not NULL_TARGET.match(expand(t, env))]
        eff["writes"] += outs
        eff["reads"] += [resolve(expand(t, env), cwd) for t in seg["in"]]
        if not words:
            continue
        eff["runs"].append(words)
        name = os.path.basename(words[0]).lower()
        name = re.sub(r"\.(exe|cmd|bat|ps1)$", "", name)
        args = words[1:]
        if name in ("cd", "pushd", "set-location", "sl", "chdir"):
            target = next((a for a in args if not a.startswith("-")), None)
            if target and target != "-":
                cwd = resolve(target, cwd)
            continue
        if INTERPRETER.match(name):
            code_flags = {"-c"} if name.startswith(("py", "pypy")) else {"-r"} if name == "php" else \
                {"-e", "--eval", "-p", "--print", "-E"}
            code, files = None, []
            for k, a in enumerate(args):
                if a in code_flags and k + 1 < len(args):
                    code = args[k + 1]
                    files = positionals(args[k + 2:])
                    break
                if a.startswith("--eval="):
                    code = a.split("=", 1)[1]
                    break
            if code is not None:
                _code_effects(code, cwd, eff)
                if name == "perl" and any(re.match(r"^-\w*i", a) for a in args):
                    eff["writes"] += [resolve(f, cwd) for f in files]  # perl -pi -e … files
            for body in seg["heredocs"]:
                _code_effects(body, cwd, eff)
            script = next((a for a in positionals(args, ("-m", "-W", "-X", "--input-type", "-r", "--require",
                                                          "--import", "--loader"))
                           if a != "-"), None) if code is None else None
            if script:
                sp = resolve(script, cwd)
                body = scripts.get(sp)
                if body is None and agent_script(sp, cwd):
                    body = read(sp)
                if body:
                    _code_effects(body, cwd, eff)
            continue
        if SHELL_CMD.match(name):
            for k, a in enumerate(args):
                if a in ("-c", "-Command", "-command") and k + 1 < len(args):
                    _effects(args[k + 1], cwd, dict(env), eff, scripts, depth + 1)
                    break
            else:
                script = next(iter(positionals(args)), None)
                sp = resolve(script, cwd) if script else None
                body = scripts.get(sp) if sp else None
                if sp and body is None and agent_script(sp, cwd):
                    body = read(sp)
                if body:
                    _effects(body, cwd, dict(env), eff, scripts, depth + 1)
            for body in seg["heredocs"]:
                _effects(body, cwd, dict(env), eff, scripts, depth + 1)
            continue
        if name == "eval":
            _effects(" ".join(args), cwd, env, eff, scripts, depth + 1)
            continue
        if seg["heredocs"] and outs:  # cat > x.py <<EOF: remember the script for a later `python x.py`
            for o in outs:
                scripts[o] = "\n".join(seg["heredocs"])
        if name == "tee":
            for f in positionals(args):
                fp = resolve(f, cwd)
                eff["writes"].append(fp)
                if seg["heredocs"]:
                    scripts[fp] = "\n".join(seg["heredocs"])
            continue
        if name == "git":
            k, cdir = 0, None
            while k < len(args) and args[k].startswith("-"):
                if args[k] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") and k + 1 < len(args):
                    if args[k] == "-C":
                        cdir = resolve(args[k + 1], cwd)
                    k += 2
                else:
                    k += 1
            if k < len(args):
                sub, rest = args[k], args[k + 1:]
                eff["git"].append({"sub": sub, "args": rest, "cwd": cdir or cwd})
                if sub in ("checkout", "restore", "rm", "mv"):
                    paths = rest[rest.index("--") + 1:] if "--" in rest else positionals(rest, ("-s", "--source"))
                    eff["writes"] += [resolve(x, cdir or cwd) for x in paths]
            continue
        pos = positionals(args, ("-s", "--size", "-t", "--target-directory", "-m", "--mode", "-o", "-g",
                                 "-Value", "-Encoding", "-ItemType", "-Filter", "-Include", "-Exclude"))
        if name in WRITERS_ALL:
            eff["writes"] += [resolve(x, cwd) for x in pos]
            continue
        if name in WRITERS_LAST and len(pos) >= 2:
            dest = resolve(pos[-1], cwd)
            srcs = [resolve(x, cwd) for x in pos[:-1]]
            eff["reads"] += srcs
            if os.path.isdir(dest) or pos[-1].endswith("/"):
                eff["writes"] += [dest.rstrip("/") + "/" + os.path.basename(s.rstrip("/")) for s in srcs]
            else:
                eff["writes"].append(dest)
            continue
        if name == "sed":
            inplace = any(a == "--in-place" or a.startswith("--in-place=") or re.match(r"^-[a-zA-Z]*i", a)
                          for a in args)
            script_given = any(a in ("-e", "-f", "--expression", "--file") for a in args)
            files = positionals(args, ("-e", "-f", "--expression", "--file"))
            files = files if script_given else files[1:]
            eff["writes" if inplace else "reads"] += [resolve(x, cwd) for x in files]
            continue
        if name == "dd":
            for a in args:
                if a.startswith("of="):
                    eff["writes"].append(resolve(a[3:], cwd))
                elif a.startswith("if="):
                    eff["reads"].append(resolve(a[3:], cwd))
            continue
        if name == "find":
            starts = []
            for a in args:
                if a.startswith("-") or a in ("(", "!"):
                    break
                starts.append(a)
            if "-delete" in args or any(args[k] in ("-exec", "-execdir") and k + 1 < len(args) and
                                        os.path.basename(args[k + 1]) in ("rm", "mv", "sed", "truncate", "shred")
                                        for k in range(len(args))):
                eff["writes"] += [resolve(x, cwd) for x in starts or ["."]]
            continue
        if name in GREPS:
            given = any(a in ("-e", "-f", "--regexp", "--file") or a.startswith("--regexp=") for a in args)
            files = positionals(args, ("-e", "-f", "--regexp", "--file", "-A", "-B", "-C", "-m", "--max-count",
                                       "-g", "--glob", "-t", "--type", "--include", "--exclude", "-Pattern", "-Path"))
            files = files if given else files[1:]
            recursive = name in ("rg", "ag", "ack") or any(re.match(r"^-[a-zA-Z]*[rR]", a) or a == "--recursive"
                                                         for a in args)
            if not files and recursive:
                files = ["."]
            eff["reads"] += [resolve(x, cwd) for x in files]
            continue
        if name in READERS:
            eff["reads"] += [resolve(x, cwd) for x in pos]


def under(root, paths, globs):
    """The paths (absolute, from shell_effects) that fall under the globs, or are folders holding
    something that does, as project-relative paths. A path with an unknown variable is judged by
    what follows the variable."""
    hits, folders = [], prefixes(globs)
    for ap in paths:
        cands = [ap]
        if "$" in ap:
            tail = re.split(r"\$\{?\w+\}?/?", ap)[-1]
            cands.append(tail)
        for c in cands:
            rel = rel_to_root(root, c) if os.path.isabs(c) else c.lstrip("./") or "."
            if rel is None:
                continue
            if rel == "." or matches(rel, globs) or matches(rel.rstrip("/") + "/x", globs) or any(f.startswith(rel.rstrip("/") + "/") for f in folders) or (
                    os.path.isdir(p(root, rel)) and any(matches(f, globs) for f in git(root, "ls-files", "--", rel).splitlines())):
                hits.append(rel)
                break
    return sorted(set(hits))


def in_project(root, paths, allowed=()):
    """Paths inside the project, except the allowed globs (scratch files outside it are fine)."""
    out = []
    for ap in paths:
        rel = rel_to_root(root, ap)
        if rel is not None and not (allowed and matches(rel, allowed)):
            out.append(rel)
    return out


def runs_dd(eff, *subcommand):
    """Does the command run `dd.py <subcommand…>`, from the shell or from inline code?"""
    for words in eff["runs"]:
        for k, w in enumerate(words):
            if w.replace("\\", "/").endswith(("dd.py", "scripts/dd")) and all(s in words[k + 1:] for s in subcommand):
                return True
    return any(SPAWN_API.search(c) and "dd.py" in c and all(s in c for s in subcommand) for c in eff["code"])


def nested_agent(eff):
    """Starting Claude Code or Codex from the shell or from inline code: it would run outside the role."""
    for words in eff["runs"]:
        name = re.sub(r"\.(exe|cmd|ps1)$", "", os.path.basename(words[0].replace("\\", "/")).lower())
        if name in ("claude", "codex") and not any(re.match(r"^--?(version|help|V|h)$", a) for a in words[1:]):
            return True
        if name in ("npx", "pnpx", "bunx") and any(re.match(r"^@(openai/codex|anthropic-ai/claude-code)\b", a)
                                                   for a in words[1:]):
            return True
    return any(SPAWN_API.search(c) and re.search(r"""['"`](?:\S*/)?(claude|codex)['"` ]""", c) for c in eff["code"])


def commit_no_verify(args):
    """`git commit --no-verify` or `-n`, also inside a cluster (-qn), but not inside a message."""
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a == "--no-verify":
            return True
        if a in ("-m", "-F", "-c", "-C", "-t", "--message", "--file", "--author", "--date", "--template",
                 "--reuse-message", "--reedit-message", "--fixup", "--squash", "--trailer", "--cleanup"):
            skip = True
            continue
        if a.startswith("-") and not a.startswith("--"):
            for k, ch in enumerate(a[1:]):
                if ch == "n":
                    return True
                if ch in "mFcCtS":
                    skip = k == len(a) - 2  # -m with its value in the next word
                    break
    return False


def effects_of(root, data):
    """shell_effects of a Bash or PowerShell tool call (cached per call), or None for other tools."""
    tool = data.get("tool_name", "")
    if tool not in ("Bash", "PowerShell"):
        return None
    cmd = (data.get("tool_input") or {}).get("command") or ""
    key = (id(data), cmd)
    if EFFECTS_CACHE.get("key") != key:
        EFFECTS_CACHE.update(key=key, eff=shell_effects(cmd, data.get("cwd") or root, powershell=tool == "PowerShell"))
    return EFFECTS_CACHE["eff"]


EFFECTS_CACHE = {}


def gate_files(root, cfg):
    """Files the gate commands run (e2e/run.sh, scripts/check.py): changing them changes the verdict."""
    out = set()
    for g in cfg["gates"]:
        for tok in re.split(r"[\s\"'=;&|()<>]+", g.get("run", "")):
            tok = tok.strip().lstrip("./").replace("\\", "/")
            if tok and not tok.startswith("-") and os.path.isfile(p(root, tok)):
                out.add(tok)
    return sorted(out)


def protected_hits(root, cfg, paths):
    """Which written paths are protected: project globs, or absolute paths outside the project."""
    rel_globs, hits = [], []
    for pt in cfg["protected"]:
        ap = os.path.expanduser(pt)
        if os.path.isabs(ap) and rel_to_root(root, ap) is None:
            ap = ap.replace("\\", "/").rstrip("/")
            hits += [x for x in paths if x == ap or x.startswith(ap + "/") or glob_re(ap).match(x.lstrip("/"))]
        else:
            rel_globs.append(pt)
    return sorted(set(hits + under(root, paths, rel_globs)))


def hook_pre_bash(root, cfg, data):
    eff = effects_of(root, data) or {"writes": [], "git": []}
    if cfg["protected"]:
        hits = protected_hits(root, cfg, eff["writes"])
        if hits:
            deny(f"Daedalus: this command writes to a protected path ({', '.join(hits[:5])}). "
                 "Protected paths are the user's deliverables: never write, move or delete them.")
    for g in eff["git"]:
        if g["sub"] == "commit":
            if rel_to_root(root, g["cwd"]) is None or find_root(g["cwd"]) not in (None, root):
                continue  # another repo's commit: its own config (if any) decides
            if commit_no_verify(g["args"]):
                deny("Daedalus: commits must run the gates; --no-verify / -n is not allowed.")
            commit_check(root, cfg)
        elif g["sub"] == "push":
            if cfg["push"] == "block":
                deny("Daedalus: pushing is disabled for this project (config push=block). Ask the user to push.")
            if cfg["push"] == "ask":
                ask("Daedalus: pushes need the user's go-ahead.")


def commit_check(root, cfg):
    """The commit hook: coverage, gates (unless only the harness's own records change), the review,
    then the requirement results, which describe the commit. A loosened config needs the user."""
    problems = coverage_problems(root, cfg)
    if problems:
        deny("Daedalus blocked this commit: requirements in .daedalus/specs/ aren't covered or aren't valid.\n- "
             + "\n- ".join(problems[:15]) + "\nSend the missing tests to the test writer (it adds "
             "`covers: <ID>`), or mark a spec not started yet as \"state\": \"planned\".")
    files = changed_files(root)
    gates = [] if harness_only(root, cfg, files) else select_gates(cfg, event="commit", files=files)
    results = []
    if gates:
        results = run_gates(root, cfg, gates, budget=HOOK_GATE_BUDGET)
        failed = [r for r in results if r["status"] == "fail"]
        if failed:
            tails = "\n\n".join(f"[{r['name']}]\n{r.get('tail', '')}" for r in failed)
            deny("Daedalus blocked this commit: a gate failed.\n" + format_results(results) +
                 "\n\nFix the cause (or revert the change) and commit again. Never weaken a gate to pass it.\n\n" + tails)
    why = review_status(root, cfg)
    if why:
        deny(why)
    if results:
        record_requirements(root, cfg, results)
    loose = config_loosened(root, cfg)
    if loose:
        ask("Daedalus: this commit loosens the harness's checks compared with the last commit: " + "; ".join(loose)
            + ". That needs the user's OK, whichever way the config was changed.")


def hook_pre_edit(root, cfg, data):
    ti = data.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path") or ti.get("path") or ""
    rel = rel_to_root(root, path) if path else None
    hits = protected_hits(root, cfg, [os.path.abspath(path).replace("\\", "/")]) if path and cfg["protected"] else []
    if hits:
        deny(f"Daedalus: {rel or path} is protected. "
             "Protected paths are the user's deliverables: never write or edit them. Ask the user instead.")
    if rel == ".daedalus/gates/baseline.json":
        deny("Daedalus: the gate baseline changes only through `dd.py gate --accept` after a passing run.")


PATCH_FILE = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): *(.+?)\s*$", re.M)


def tool_calls(data):
    """The tool call in Claude Code's terms, as one or more payloads. Codex edits files with
    apply_patch (one patch, many files: one Edit per file, paths relative to the session's cwd) and
    starts agents with a (namespaced) spawn_agent tool."""
    tool = data.get("tool_name", "")
    if HOST["name"] != "codex":
        return [data]
    if tool == "apply_patch":
        ti = data.get("tool_input") or {}
        patch = ti.get("command") or ti.get("patch") or ti.get("input") or ""
        cwd = data.get("cwd") or ""
        paths = PATCH_FILE.findall(patch if isinstance(patch, str) else "")
        return [{**data, "tool_name": "Edit", "tool_input": {"file_path": os.path.join(cwd, x)}} for x in paths]
    if tool.endswith("spawn_agent"):
        return [{**data, "tool_name": "Agent"}]
    return [data]


def hook_pre_tool(root, cfg, data):
    """Every tool call: the context budget first, then the tool's own rules."""
    items = tool_calls(data)
    if not items:
        deny("Daedalus: couldn't tell which files this patch changes, so it's blocked. Use the standard "
             "`*** Add File:` / `*** Update File:` / `*** Delete File:` headers.")
    typed = queued_typed(root, cfg, data)
    verdict, msg = context_check(root, cfg, data, items)
    if verdict == "deny":
        deny(msg)
    for d in items:
        kind, why = integrity_check(root, cfg, d)
        if kind == "deny":
            deny(why)
        if kind == "ask":
            ask(why, d)
        why = role_check(root, cfg, d)
        if why:
            deny(why)
        tool = d.get("tool_name", "")
        if tool in ("Bash", "PowerShell"):
            hook_pre_bash(root, cfg, d)
        elif tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            hook_pre_edit(root, cfg, d)
    notes = (typed if role_of(data) == "main" else []) + ([msg] if verdict == "note" else [])
    if notes:
        emit_context("PreToolUse", "\n".join(notes))


def pause_pending(root, cfg, data):
    """Over the hard limit and no handoff written since: the pause isn't finished."""
    if role_of(data) != "main":
        return None
    pct, _ = context_pct(cfg, data)
    if pct is None or pct < pause_at(cfg):
        return None
    st = ctx_state(root, data.get("session_id", ""))
    if not st.get("limit_hit_at"):
        st.update(limit_hit_at=time.time(), pct=pct)
        save_json(ctx_state_path(root), st)
    return None if pause_done(root, cfg, st) else pct


def hook_user_prompt(root, cfg, data):
    if role_of(data) != "main":
        return
    notes = queued_typed(root, cfg, data)
    if HOST["name"] == "codex":
        # In Codex this hook fires only for a person's input (agents talk to each other through tool
        # calls, and a subagent's turn carries its agent_id), so typed checks, changes and approvals count.
        prompt = data.get("prompt") or ""
        if re.match(r"\s*[$/]?daedalus:(verify|change)\b", prompt, re.I):
            notes += record_typed(root, cfg, re.sub(r"^\s*daedalus:", "$daedalus:", prompt), "user (typed in Codex)")
        reply = user_reply_to_ask(root, data)
        if reply:
            notes.append(reply)
    pct, _ = context_pct(cfg, data)
    if pct is not None and pct >= pause_at(cfg):
        st = ctx_state(root, data.get("session_id", ""))
        resume = f"{cmd_name('start')} continue"
        if pause_done(root, cfg, st):
            notes.append(f"Daedalus: the context is at {pct}%, over the {pause_at(cfg)}% limit, and the work "
                         f"is saved. Don't start new work: ask the user to {clear_hint()} and then `{resume}`.")
        else:
            notes.append(f"Daedalus: the context is at {pct}%, over the {pause_at(cfg)}% limit. Don't start new "
                         f"work: run {cmd_name('pause')} now, then ask the user to {clear_hint()} and then `{resume}`.")
    if notes:
        emit_context("UserPromptSubmit", "\n".join(notes))


def hook_prompt_expansion(root, cfg, data):
    """Claude Code's UserPromptExpansion fires only for a command the user typed, so it's where a
    manual check gets recorded: `/daedalus:verify UI-3 ok`. (UserPromptSubmit also fires for subagent
    reports and scheduled prompts, so it can't tell a person from an agent.)"""
    if role_of(data) != "main":
        return
    name = (data.get("command_name") or "").lower().lstrip("/")
    prompt = data.get("prompt") or ""
    if name not in ("daedalus:verify", "daedalus:change") and not re.match(r"\s*/daedalus:(verify|change)\b", prompt, re.I):
        return
    if not re.match(r"\s*/daedalus:(verify|change)\b", prompt, re.I):
        args = data.get("command_args")
        prompt = f"/{name} {args if isinstance(args, str) else ''}"
    notes = record_typed(root, cfg, prompt, "user (typed the command)")
    if notes:
        emit_context("UserPromptExpansion", "\n".join(notes))


def hook_stop(root, cfg, data):
    if role_of(data) != "main":
        return
    typed = queued_typed(root, cfg, data)
    if typed:  # the user wrote while the session worked: the agent acknowledges it before stopping
        emit({"decision": "block", "reason": "\n".join(typed)})
    again = data.get("stop_hook_active")  # an earlier Stop hook already sent the session back once
    pct = None if again else pause_pending(root, cfg, data)
    if pct is not None:
        emit({"decision": "block", "reason":
              f"Daedalus: the context is at {pct}%, over the {pause_at(cfg)}% limit, and the work "
              f"isn't saved yet. Run {cmd_name('pause')}: gates, commit if green, status file, handoff (with the "
              f"resume prompt), board. Then end with: \"{clear_hint().capitalize()}, then: {cmd_name('start')} continue\"."})
    # A build-all run nudges even when `again` is set: it loops only while new commits keep coming,
    # and ends itself on the first stop without one.
    nudge = run_nudge(root, cfg, data)
    if nudge:
        emit(nudge)
    if again or cfg["stop"] == "off":
        return
    unsaved = unsaved_commits(root, cfg)
    if not unsaved:
        return
    msg = (f"Daedalus: {len(unsaved)} commit(s) since the last handoff ({'; '.join(unsaved[:3])}). "
           f"Run the save step ({cmd_name('save')}) before stopping, so no progress lives only in this conversation.")
    if cfg["stop"] == "block":
        emit({"decision": "block", "reason": msg})
    emit({"systemMessage": msg})


def hook_session_start(root, cfg, data):
    ensure_gitignore(root)
    seen = load_json(hosts_seen_path(root), {})  # for `dd.py doctor`: proof the hooks run on this host
    seen[HOST["name"]] = dt.datetime.now().isoformat(timespec="seconds")
    save_json(hosts_seen_path(root), seen)
    text = resume_text(root, cfg)
    src = data.get("source", "startup")
    if src == "compact":
        lead = (f"Daedalus: the context was compacted to stay under {cfg['context']['limit_pct']}% of the window. "
                "Re-read the saved state below, check it against git, and carry on with the task you were doing. "
                "The user doesn't need to clear or resume anything.\n\n")
    else:
        lead = ("This project runs on the Daedalus harness. Before other work, reply with three lines: "
                "Where we are / Next / Blocked on, from the state below (verify against git if they disagree), "
                "unless the user already gave a command.\n\n")
    if HOST["name"] == "codex":
        lead += ("Daedalus in Codex: the skills are written for both hosts. Where they say `/daedalus:<skill>`, "
                 "the command here is `$daedalus:<skill>`. Where they say to spawn `daedalus:<role>` (subagent_type), "
                 "spawn the Codex agent `daedalus-<role>`: daedalus-builder, daedalus-test-writer, "
                 "daedalus-reviewer, daedalus-researcher. AskUserQuestion means: ask in the chat.\n\n")
    emit_context("SessionStart", lead + text)


def hook_pre_compact(root, cfg, data):
    if role_of(data) == "main":  # evidence for `dd.py doctor` that compaction really happens
        pct, _ = context_pct(cfg, data)
        with open(p(root, ".daedalus/compactions.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"), "host": HOST["name"],
                                "trigger": data.get("trigger", ""), "pct": pct}) + "\n")
    hook_snapshot(root, cfg, data, "before context compaction")


def hook_snapshot(root, cfg, data, reason):
    if role_of(data) != "main":
        return
    if cfg["auto_snapshot"] and needs_snapshot(root, cfg):
        path = snapshot(root, cfg, reason)
        print(f"Daedalus: wrote {os.path.relpath(path, root)}", file=sys.stderr)


def agents_dir(root):
    return p(root, ".daedalus/agents")


def hook_subagent_start(root, cfg, data):
    role = role_of(data)
    if role not in ROLES:
        return
    save_json(os.path.join(agents_dir(root), f"{data.get('agent_id', 'unknown')}.json"),
              {"role": role, "started": time.time()})
    emit_context("SubagentStart", agent_note(root, cfg, role))


def hook_subagent_stop(root, cfg, data):
    """A reviewer can't finish without recording a verdict (once: stop_hook_active ends the loop)."""
    if role_of(data) != "reviewer" or data.get("stop_hook_active"):
        return
    st = load_json(os.path.join(agents_dir(root), f"{data.get('agent_id', 'unknown')}.json"), {})
    started = st.get("started", 0)
    recorded = [f for f in glob.glob(p(root, ".daedalus/reviews/*.json"))
                if os.path.basename(f) != "draft.json" and os.path.getmtime(f) > started]
    if not recorded:
        emit({"decision": "block", "reason": f"Daedalus: record your verdict before finishing: write {REVIEW_DRAFT} "
                                              f"and run `python \"{DD}\" review-record`."})


def hook(event):
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    root = find_root(data.get("cwd"))
    if not root:
        return  # not a Daedalus project: stay out of the way
    HOOK_CTX.update(root=root, data=data)
    try:
        run_hook(event, root, data)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — any failure inside a guard must not let the action through
        msg = f"Daedalus hook error in {event}: {type(e).__name__}: {e}"
        if event in ("pre-tool", "pre-bash", "pre-edit") and os.environ.get("DAEDALUS_FAIL_OPEN") != "1":
            deny(msg + ". Failing closed: the action is blocked until the harness works again. Check "
                 ".daedalus/config.json; set DAEDALUS_FAIL_OPEN=1 to bypass while you fix it.")
        print(msg, file=sys.stderr)  # other events never trap the session


def run_hook(event, root, data):
    cfg = load_config(root)
    handlers = {"session-start": hook_session_start, "pre-tool": hook_pre_tool, "pre-bash": hook_pre_bash,
                "pre-edit": hook_pre_edit, "user-prompt": hook_user_prompt, "stop": hook_stop,
                "user-prompt-expansion": hook_prompt_expansion,
                "subagent-start": hook_subagent_start, "subagent-stop": hook_subagent_stop,
                "pre-compact": hook_pre_compact,
                "session-end": lambda r, c, d: hook_snapshot(r, c, d, f"session end: {d.get('reason', '')}")}
    handlers[event](root, cfg, data)


# --- detect: what the project is, so setup can propose everything --------------------------

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", ".next", "target",
             ".daedalus", ".pytest_cache", ".mypy_cache", ".ruff_cache", "coverage", ".tox", ".idea", ".vscode"}
WEB_DEPS = ("next", "react", "vue", "svelte", "@sveltejs/kit", "vite", "express", "fastify", "nuxt", "astro",
            "@remix-run/react", "hono", "koa", "@angular/core")
PY_WEB = ("fastapi", "flask", "django", "starlette", "streamlit", "gradio")
SECRETS = [".env", ".env.local", ".env.*.local", ".env.production", "*.pem", "*.key", "**/secrets/**"]


def project_files(root, limit=20000):
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".") or x in (".github",)]
        rel = os.path.relpath(d, root).replace("\\", "/")
        for fn in files:
            out.append(fn if rel == "." else f"{rel}/{fn}")
            if len(out) >= limit:
                return out
    return out


def detect(root):
    """A proposal for the harness config, from the project's own files. Nothing is written."""
    files = project_files(root)
    has = set(files)
    top = {f.split("/")[0] for f in files if "/" in f}
    text = lambda rel: read(p(root, rel), 200_000)  # noqa: E731
    stack, gates, notes, src, tests_globs = [], [], [], [], list(DEFAULTS["roles"]["tests"])
    kind = "unknown"
    pm = None

    if "package.json" in has:
        try:
            pkg = json.loads(text("package.json") or "{}")
        except ValueError:
            pkg = {}
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        scripts = pkg.get("scripts") or {}
        pm = ("pnpm" if "pnpm-lock.yaml" in has else "yarn" if "yarn.lock" in has else
              "bun" if {"bun.lockb", "bun.lock"} & has else "npm")
        stack.append(f"JavaScript/TypeScript ({pm})")
        if any(d in deps for d in WEB_DEPS):
            kind = "web app"
        elif pkg.get("bin"):
            kind = "command-line tool"
        test = scripts.get("test", "")
        if test and "no test specified" not in test:
            gates.append({"name": "tests", "run": f"{pm} run test", "when": ["commit"], "timeout": 900})
        if scripts.get("lint"):
            gates.append({"name": "lint", "run": f"{pm} run lint", "when": ["commit"], "timeout": 300})
        tc = next((k for k in ("typecheck", "type-check", "check-types", "tsc") if scripts.get(k)), None)
        if tc:
            gates.append({"name": "types", "run": f"{pm} run {tc}", "when": ["commit"], "timeout": 300})
        elif "tsconfig.json" in has and "typescript" in deps:
            gates.append({"name": "types", "run": "npx tsc --noEmit", "when": ["commit"], "timeout": 300})
        pw = next((f for f in has if re.fullmatch(r"playwright\.config\.(ts|js|mjs|cjs)", f)), None)
        if pw:
            m = re.search(r"testDir\s*:\s*['\"]\.?/?([^'\"]+)['\"]", text(pw))
            tdir = (m.group(1).strip("/") if m else "tests")
            gates.append({"name": "e2e", "run": "npx playwright test", "when": ["commit"], "timeout": 1200,
                          "evidence": "e2e", "tests": [f"{tdir}/**"]})
        elif any(re.fullmatch(r"cypress\.config\.(ts|js|mjs|cjs)", f) for f in has):
            gates.append({"name": "e2e", "run": "npx cypress run", "when": ["commit"], "timeout": 1200,
                          "evidence": "e2e", "tests": ["cypress/e2e/**"]})
        src += [f"{d}/**" for d in ("src", "app", "pages", "components", "lib", "server", "api", "routes")
                if d in top]

    py_files = [f for f in files if f.endswith(".py")]
    if {"pyproject.toml", "setup.py", "setup.cfg"} & has or any(f.startswith("requirements") and f.endswith(".txt")
                                                               for f in has) or py_files:
        pyproject = text("pyproject.toml")
        reqs = " ".join(text(f) for f in has if f.startswith("requirements") and f.endswith(".txt"))
        alldeps = (pyproject + reqs).lower()
        runner = "uv run " if "uv.lock" in has else "poetry run " if "poetry.lock" in has else "python -m "
        stack.append("Python" + (" (uv)" if runner == "uv run " else " (poetry)" if runner == "poetry run " else ""))
        if any(w in alldeps for w in PY_WEB):
            kind = "web app" if kind == "unknown" else kind
        elif "[project.scripts]" in pyproject or "console_scripts" in alldeps:
            kind = "command-line tool" if kind == "unknown" else kind
        py_tests = [f for f in py_files if re.search(r"(^|/)(test_[^/]*|[^/]*_test)\.py$", f)]
        if "pytest" in alldeps or py_tests or "conftest.py" in {f.split("/")[-1] for f in has}:
            run = (runner + "pytest -q") if runner != "python -m " else "python -m pytest -q"
            if not any(g["name"] == "tests" for g in gates):
                gates.append({"name": "tests", "run": run, "when": ["commit"], "timeout": 900})
        if "ruff" in alldeps or "ruff.toml" in has or "[tool.ruff" in pyproject:
            gates.append({"name": "lint", "run": (runner if runner != "python -m " else "") + "ruff check .",
                          "when": ["commit"], "timeout": 300})
        pkgs = sorted({f.split("/")[0] for f in py_files if f.count("/") == 1 and f.endswith("/__init__.py")}
                      - {"tests", "test"})
        src += ["src/**"] if "src" in top else [f"{d}/**" for d in pkgs]
        if not src:
            src += ["*.py"]

    if "go.mod" in has:
        stack.append("Go")
        gates.append({"name": "tests", "run": "go test ./...", "when": ["commit"], "timeout": 900})
        gates.append({"name": "vet", "run": "go vet ./...", "when": ["commit"], "timeout": 300})
        src += ["**/*.go"]
        tests_globs.append("**/*_test.go")
    if "Cargo.toml" in has:
        stack.append("Rust")
        gates.append({"name": "tests", "run": "cargo test", "when": ["commit"], "timeout": 1200})
        src += ["src/**"]
    if not any(g["name"] == "tests" for g in gates) and "Makefile" in has and re.search(r"^test:", text("Makefile"), re.M):
        gates.append({"name": "tests", "run": "make test", "when": ["commit"], "timeout": 900})

    # one entry per gate name, first wins
    seen, uniq = set(), []
    for g in gates:
        if g["name"] not in seen:
            seen.add(g["name"])
            uniq.append(g)
    gates = uniq
    test_files = [f for f in files if matches(f, tests_globs)]
    if not stack:
        notes.append("This folder has no code yet. The first milestone sets up the project and its checks.")
    if not any(g["name"] == "tests" for g in gates):
        notes.append("No test command found. Milestone 0 adds a test runner and the first tests, so every later "
                     "step can be checked automatically.")
    elif not test_files:
        notes.append("A test command exists but there are no tests yet; the first steps add them.")
    if not any(g.get("evidence") == "e2e" for g in gates):
        notes.append({"web app": "No end-to-end check yet. For a web app that's a Playwright test that opens the "
                                 "real pages: Milestone 0 adds it.",
                      "command-line tool": "No end-to-end check yet: Milestone 0 adds a script that runs the real "
                                           "command on sample input.",
                      }.get(kind, "No end-to-end check yet: Milestone 0 adds one that runs the real program the "
                                  "way a user would."))
    return {"stack": stack or ["none yet"], "kind": kind, "package_manager": pm, "gates": gates,
            "roles": {"source": sorted(set(src)) or ["src/**"], "tests": tests_globs},
            "review": {"required_for": sorted(set(src)) or ["src/**"]},
            "protected": SECRETS, "test_files": len(test_files), "notes": notes,
            "git": os.path.isdir(os.path.join(root, ".git"))}


# --- host settings: compaction and hooks ----------------------------------------------------

def claude_settings_path(root):
    return os.path.join(root, ".claude", "settings.json")


def setup_hosts(root, cfg):
    """Write what each agent host needs for this project. Returns plain-language lines."""
    out = []
    c = cfg["context"]
    path = claude_settings_path(root)
    data = load_json(path, None) if os.path.exists(path) else {}
    if data is None:
        out.append(f"! {os.path.relpath(path, root)} isn't valid JSON, so I left it alone. Fix it, then run setup again.")
    else:
        env = data.setdefault("env", {})
        if compacting(cfg):
            env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(c["limit_pct"])
            out.append(f"Claude Code compacts this project's sessions at {c['limit_pct']}% "
                       f"({os.path.relpath(path, root)}).")
        else:
            env.pop("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", None)
            out.append("Claude Code: compaction left at its default (on_limit is \"pause\").")
        if not env:
            data.pop("env")
        save_json(path, data)
    out += setup_codex_project(root, cfg)
    return out


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def codex_wanted(root):
    return bool(shutil.which("codex")) or os.path.isdir(os.path.join(root, ".codex"))


def toml_top_value(text, key):
    """A top-level `key = value` (before the first [table]), or None."""
    for line in text.splitlines():
        if line.lstrip().startswith("["):
            return None
        m = re.match(rf"\s*{re.escape(key)}\s*=\s*(.+?)\s*(#.*)?$", line)
        if m:
            return m.group(1).strip().strip('"')
    return None


def toml_set_top(text, key, value, comment=""):
    """Set a top-level key, keeping everything else (tables stay below it)."""
    lines = text.splitlines()
    first_table = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("[")), len(lines))
    while first_table and not lines[first_table - 1].strip():  # keep the blank line before the tables
        first_table -= 1
    new = f"{key} = {value}" + (f"  # {comment}" if comment else "")
    for i in range(first_table):
        if re.match(rf"\s*{re.escape(key)}\s*=", lines[i]):
            lines[i] = new
            return "\n".join(lines) + "\n"
    lines.insert(first_table, new)
    return "\n".join(lines) + "\n"


def toml_set_in_table(text, table, key, value):
    """Set `key = value` inside [table], adding the table at the end if it's missing."""
    lines = text.rstrip("\n").splitlines() if text.strip() else []
    head = next((i for i, ln in enumerate(lines) if ln.strip() == f"[{table}]"), None)
    if head is None:
        return "\n".join(lines + ([""] if lines else []) + [f"[{table}]", f"{key} = {value}"]) + "\n"
    end = next((i for i in range(head + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    for i in range(head + 1, end):
        if re.match(rf"\s*{re.escape(key)}\s*=", lines[i]):
            lines[i] = f"{key} = {value}"
            return "\n".join(lines) + "\n"
    lines.insert(end, f"{key} = {value}")
    return "\n".join(lines) + "\n"


def codex_window():
    """(model, context window in tokens) of the user's Codex model, from `codex debug models`.
    Without a configured model, the smallest window listed (compacting early is the safe side)."""
    model = toml_top_value(read(os.path.join(codex_home(), "config.toml")), "model")
    exe = shutil.which("codex")
    if exe:
        try:
            r = subprocess.run([exe, "debug", "models"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60, stdin=subprocess.DEVNULL)
            models = json.loads(r.stdout or "{}").get("models") or []
        except (OSError, ValueError, subprocess.TimeoutExpired):
            models = []
        sized = [m for m in models if m.get("context_window")]
        hit = next((m for m in sized if m.get("slug") == model), None)
        if hit:
            return model, hit["context_window"]
        if sized and not model:
            m = min(sized, key=lambda x: x["context_window"])
            return m["slug"], m["context_window"]
    return model or "unknown", 272_000


def codex_agent_toml(role):
    """A Codex agent role from the plugin's agent file: same instructions, Codex's format."""
    text = read(os.path.join(PLUGIN_ROOT, "agents", f"{role}.md"))
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    meta, body = (m.group(1), m.group(2)) if m else ("", text)
    desc = re.search(r'^description:\s*"?(.*?)"?\s*$', meta, re.M)
    body = ("You run as a Codex subagent started by the Daedalus orchestrator. The Daedalus CLI path and your "
            "role's rules are in your start context. Your report back is your final message.\n\n" + body.strip())
    lines = [f'# Written by Daedalus (`dd.py setup`) from agents/{role}.md. Run setup again to update it.',
             f'name = "daedalus-{role}"',
             f"description = {json.dumps(desc.group(1) if desc else role)}",
             'model_reasoning_effort = "high"' if role == "reviewer" else 'model_reasoning_effort = "medium"',
             "developer_instructions = '''", body.replace("'''", "'' '"), "'''", ""]
    return "\n".join(lines)


AGENTS_MARK = ("<!-- daedalus:start -->", "<!-- daedalus:end -->")


def setup_codex_project(root, cfg):
    """Codex reads project settings from .codex/ (trusted projects only) and instructions from AGENTS.md."""
    if not codex_wanted(root):
        return ["Codex: not installed here, skipped (run setup again after installing it)."]
    out = []
    path = os.path.join(root, ".codex", "config.toml")
    text = read(path)
    if compacting(cfg):
        model, window = codex_window()
        pct = cfg["context"]["limit_pct"]
        limit = int(window * 0.95 * pct / 100)  # Codex counts the usable 95% of the window
        text = toml_set_top(text, "model_auto_compact_token_limit", str(limit),
                            f"Daedalus: {pct}% of {model} ({window:,} tokens); dd.py setup")
        text = toml_set_top(text, "model_post_turn_compact_threshold_percent", str(pct), "Daedalus")
        out.append(f"Codex compacts at {limit:,} tokens ({pct}% of {model}) and after a turn past {pct}% "
                   "(.codex/config.toml).")
    if os.name == "nt":
        # Codex's Windows sandbox runs commands as another user, and git refuses a repository owned by
        # someone else ("dubious ownership"), so every commit would fail. Trust this one folder, through
        # the environment of the commands Codex runs here, and nothing else.
        if toml_top_value(text, "GIT_CONFIG_COUNT") is None and "GIT_CONFIG_COUNT" not in text:
            t = "shell_environment_policy.set"
            text = toml_set_in_table(text, t, "GIT_CONFIG_COUNT", '"1"')
            text = toml_set_in_table(text, t, "GIT_CONFIG_KEY_0", '"safe.directory"')
            text = toml_set_in_table(text, t, "GIT_CONFIG_VALUE_0", json.dumps(root.replace("\\", "/")))
            out.append("Codex's Windows sandbox may run git as another user: git trusts this folder for it.")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    for role in ROLES:
        ap = os.path.join(root, ".codex", "agents", f"daedalus-{role}.toml")
        os.makedirs(os.path.dirname(ap), exist_ok=True)
        with open(ap, "w", encoding="utf-8") as f:
            f.write(codex_agent_toml(role))
    out.append("Codex agents: daedalus-builder, -test-writer, -reviewer, -researcher (.codex/agents/).")
    md = os.path.join(root, "AGENTS.md")
    block = (f"{AGENTS_MARK[0]}\n## Daedalus harness\n\n"
             "This project runs on the Daedalus build harness; hooks enforce its rules.\n\n"
             "- Every session starts with a Daedalus hook adding the project state (`# Daedalus: …`) to your "
             "context. **If you don't see it, the hooks aren't running.** Before anything else, tell the user: "
             "in Codex, trust this project folder and the Daedalus hooks (`/hooks`); in Claude Code, enable the "
             "daedalus plugin. Then `python <daedalus>/scripts/dd.py doctor` checks the setup.\n"
             "- Start with `$daedalus:start` in Codex, `/daedalus:start` in Claude Code.\n"
             f"{AGENTS_MARK[1]}\n")
    old = read(md)
    if AGENTS_MARK[0] in old:
        new = re.sub(re.escape(AGENTS_MARK[0]) + r".*?" + re.escape(AGENTS_MARK[1]) + r"\n?", block, old, flags=re.S)
    else:
        new = (old.rstrip() + "\n\n" if old.strip() else "") + block
    with open(md, "w", encoding="utf-8") as f:
        f.write(new)
    out.append("AGENTS.md tells Codex what to do if the Daedalus hooks aren't running.")
    return out


def hosts_seen_path(root):
    return p(root, ".daedalus/hosts.json")


def doctor_codex(root, cfg):
    """(good, bad, info) for Codex, if it's used here."""
    if not codex_wanted(root):
        return [], [], ["Codex isn't installed: skipped its checks"]
    good, bad, info = [], [], []
    text = read(os.path.join(root, ".codex", "config.toml"))
    if compacting(cfg):
        limit = toml_top_value(text, "model_auto_compact_token_limit")
        (good if limit else bad).append(f"Codex compacts at {int(limit):,} tokens (.codex/config.toml)" if limit else
                                        f"Codex won't compact at {cfg['context']['limit_pct']}% → run "
                                        f"`python \"{DD}\" setup`")
    missing = [r for r in ROLES if not os.path.exists(os.path.join(root, ".codex", "agents", f"daedalus-{r}.toml"))]
    (bad if missing else good).append(f"Codex agents missing: {', '.join(missing)} → run `python \"{DD}\" setup`"
                                      if missing else "Codex agents: builder, test writer, reviewer, researcher")
    user = read(os.path.join(codex_home(), "config.toml"))
    norm = lambda s: s.replace("\\\\", "\\").replace("/", "\\").lower()  # noqa: E731
    trusted = any(norm(root) in norm(ln) for ln in user.splitlines() if ln.lstrip().startswith("[projects"))
    if trusted and re.search(r'trust_level\s*=\s*"trusted"', user):
        good.append("Codex trusts this project (its .codex/ settings load)")
    else:
        bad.append("Codex doesn't trust this folder yet → open Codex here and answer yes when it asks to trust it "
                   "(without that, .codex/ is ignored)")
    plugin = re.search(r'\[plugins\."daedalus@[^"]*"\][^\[]*enabled\s*=\s*true', user)
    if plugin:
        good.append("Daedalus plugin enabled in Codex")
    else:
        bad.append("the Daedalus plugin isn't enabled in Codex → `codex plugin marketplace add <daedalus folder>` "
                   "then `codex plugin add daedalus@daedalus`")
    seen = load_json(hosts_seen_path(root), {}).get("codex")
    if seen:
        info.append(f"Codex hooks last ran here {seen}")
    else:
        info.append("Codex hooks haven't run here yet. Start Codex in this folder: if it doesn't greet you with "
                    "the Daedalus state, open /hooks and trust the Daedalus hooks (Codex skips hooks you haven't "
                    "trusted, and again after they change)")
    return good, bad, info


def doctor(root, cfg):
    """(ok, lines): does the harness work here? Plain language, with the fix for each problem."""
    good, bad, info = [], [], []
    v = sys.version_info
    (good if v >= (3, 10) else bad).append(f"Python {v.major}.{v.minor}" + ("" if v >= (3, 10) else
                                                                           " is too old → install Python 3.10 or newer"))
    if git(root, "rev-parse", "--is-inside-work-tree") == "true":
        good.append("git repository")
    else:
        bad.append("not a git repository → run `git init` here: every finished step is saved as a commit")
    bash = find_bash()
    if bash:
        good.append(f"bash for the check commands ({bash})")
    else:
        bad.append("no bash found → install Git for Windows (it brings Git Bash)" if os.name == "nt" else
                   "no bash found → install bash")
    if not cfg["gates"]:
        bad.append("no checks configured → run setup again, or add a gate to .daedalus/config.json")
    for g in cfg["gates"]:
        run = g.get("run", "")
        if "replace with your test command" in run:
            bad.append(f"check '{g['name']}' is still the placeholder → set its real command")
            continue
        word = re.split(r"\s+", run.strip(), 1)[0] if run.strip() else ""
        if word and bash and "=" not in word and not re.match(r"^(\.|/|[A-Za-z]:|bash$|sh$|cd$|echo$|exit$)", word):
            code, _ = run_shell(f"command -v {word}", root, 20)
            if code != 0:
                bad.append(f"check '{g['name']}' runs `{word}`, which isn't installed or isn't on PATH")
                continue
        good.append(f"check '{g['name']}': `{run}`")
    if cfg["requirements"].get("milestone_needs_e2e", True) and not e2e_gates(cfg):
        info.append("no end-to-end check yet: milestones can't be closed until one exists (Milestone 0 adds it)")
    if compacting(cfg):
        env = (load_json(claude_settings_path(root), {}) or {}).get("env") or {}
        want = str(cfg["context"]["limit_pct"])
        if env.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE") == want:
            good.append(f"Claude Code compacts at {want}% (.claude/settings.json)")
        else:
            bad.append(f"Claude Code won't compact at {want}% → run `python \"{DD}\" setup`")
    g2, b2, i2 = doctor_codex(root, cfg) or ([], [], [])
    good, bad, info = good + g2, bad + b2, info + i2
    log = p(root, ".daedalus/compactions.jsonl")
    if os.path.exists(log):
        last = read(log).strip().splitlines()[-1:]
        if last:
            d = json.loads(last[0])
            info.append(f"last compaction: {d.get('at')} ({d.get('host')}, at {d.get('pct')}%)")
    waiting = [x[1]["id"] for x in try_items(root, cfg) if x[1].get("verify") == "manual" and not x[2].get("passes")]
    if waiting:
        info.append(f"waiting for you to try: {', '.join(waiting)} (`python \"{DD}\" try`)")
    lines = [f"✓ {x}" for x in good] + [f"✗ {x}" for x in bad] + [f"· {x}" for x in info]
    lines.append("All good." if not bad else f"{len(bad)} problem(s) to fix.")
    return not bad, lines


LOCAL_STATE = ["gates/last.json", "gates/history.jsonl", "gates/passed.json", "gates/.lock", "context.json",
               "compactions.jsonl", "hosts.json", "approvals.json", "hooks.log", "agents/", "reviews/draft.json",
               "run.json", "typed.json"]


def ensure_gitignore(root):
    """.daedalus/.gitignore lists the local run state (the gate baseline and results are committed);
    entries added by newer versions are appended, the rest is left alone."""
    gi = p(root, ".daedalus/.gitignore")
    text = read(gi) or "# Local run state; the gate baseline is committed.\n"
    missing = [x for x in LOCAL_STATE if x not in text.splitlines()]
    if missing:
        os.makedirs(os.path.dirname(gi), exist_ok=True)
        with open(gi, "w", encoding="utf-8") as f:
            f.write(text.rstrip("\n") + "\n" + "\n".join(missing) + "\n")


# --- init -----------------------------------------------------------------------------------

def init(name, auto=False):
    root = os.path.abspath(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    target = os.path.join(root, ".daedalus")
    if os.path.exists(os.path.join(target, "config.json")):
        sys.exit(f"{target} already has a config.json; edit it instead.")
    tpl = os.path.join(PLUGIN_ROOT, "templates")
    for dirpath, _, files in os.walk(tpl):
        for fn in files:
            src = os.path.join(dirpath, fn)
            dst = os.path.join(target, os.path.relpath(src, tpl))
            if os.path.exists(dst):
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(src, encoding="utf-8") as f:
                text = f.read().replace("{{PROJECT}}", name or os.path.basename(root))
            with open(dst, "w", encoding="utf-8") as f:
                f.write(text)
    ensure_gitignore(root)
    if not auto:
        print(f"Created {target}. Next: fill config.json (gates, protected) and plan.md.")
        return
    found = detect(root)
    path = os.path.join(target, "config.json")
    cfg = load_json(path, {})
    cfg.update(gates=found["gates"], protected=found["protected"])
    cfg["roles"] = {**cfg.get("roles", {}), **found["roles"]}
    cfg["review"] = {**cfg.get("review", {}), **found["review"]}
    save_json(path, cfg)
    lines = setup_hosts(root, load_config(root))
    print(json.dumps({"created": target, "detected": found, "hosts": lines}, indent=2, ensure_ascii=False))


# --- main -----------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    it = sub.add_parser("init")
    it.add_argument("--name")
    it.add_argument("--auto", action="store_true", help="fill gates, roles and protected paths from detect, and set up the hosts")
    sub.add_parser("detect", help="propose gates, roles and protected paths from the project files (JSON)")
    sub.add_parser("setup", help="write the agent hosts' settings for this project (compaction)")
    sub.add_parser("doctor", help="check that the harness works here, in plain language")
    sub.add_parser("resume")
    sub.add_parser("status")
    g = sub.add_parser("gate")
    g.add_argument("names", nargs="*")
    g.add_argument("--all", action="store_true", help="include manual-only gates")
    g.add_argument("--accept", action="store_true", help="store passing metrics as the new baseline")
    g.add_argument("--fresh", action="store_true", help="run every gate even if it passed on exactly these files")
    s = sub.add_parser("snapshot")
    s.add_argument("--reason", default="manual")
    sub.add_parser("review-record", help="the reviewer agent: validate and stamp .daedalus/reviews/draft.json")
    sub.add_parser("requirements", help="every requirement: verify kind, coverage, pass/fail (from the gates)")
    ms = sub.add_parser("milestone", help="is this milestone's gate met? (exit 0 = done)")
    ms.add_argument("name")
    rn = sub.add_parser("run", help="the build-all run: start it, end it, or show the milestones left")
    rn.add_argument("action", nargs="?", choices=["status", "start", "end"], default="status")
    rn.add_argument("--reason", default="", help="(end) why the run ends: all done, or what waits on the user")
    chg = sub.add_parser("change", help="changes to what gets built: list them, add one (the user), or mark one planned")
    chg.add_argument("action", nargs="?", choices=["list", "add", "done"], default="list")
    chg.add_argument("what", nargs="*", help="(add) the change, in words; (done) its number")
    chg.add_argument("--where", default="", help="(done) where it went in the plan, e.g. M9")
    chg.add_argument("--by", default="user (terminal)", help="(add) who asked for it")
    vm = sub.add_parser("verify-manual", help="(the user only) mark a manual requirement as verified")
    vm.add_argument("id")
    vm.add_argument("--note", required=True, help="what you checked, and how")
    vm.add_argument("--fail", action="store_true", help="record it as failing instead")
    tr = sub.add_parser("try", help="what you can try yourself, and the checks only you can record")
    tr.add_argument("which", nargs="?", help="a milestone (M1) or a requirement ID")
    tr.add_argument("--ask", action="store_true", help="(the user, in a terminal) walk through each check and record it")
    sub.add_parser("review-status", help="does the current change set need a review, and is there one?")
    cx = sub.add_parser("context")
    cx.add_argument("transcript")
    h = sub.add_parser("handoff-name")
    h.add_argument("slug")
    k = sub.add_parser("hook")
    k.add_argument("event", choices=["session-start", "pre-tool", "pre-bash", "pre-edit", "user-prompt", "stop",
                                         "user-prompt-expansion", "subagent-start", "subagent-stop", "pre-compact",
                                         "session-end"])
    k.add_argument("--host", choices=["claude", "codex"], default=os.environ.get("DAEDALUS_HOST") or "claude",
                   help="the agent running the hook (default claude)")
    a = ap.parse_args()

    if a.cmd == "hook":
        HOST["name"] = a.host
        hook(a.event)
        return
    if a.cmd == "init":
        init(a.name, a.auto)
        return
    if a.cmd == "detect":
        print(json.dumps(detect(find_root() or os.path.abspath(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())),
                         indent=2, ensure_ascii=False))
        return
    if a.cmd == "context":
        root = find_root()
        cfg = load_config(root) if root else {**DEFAULTS}
        tokens, model = context_tokens(a.transcript)
        if tokens is None:
            sys.exit("No usage records in that transcript yet.")
        w = context_window(cfg, tokens, model)
        c = cfg["context"]
        print(f"{tokens:,} / {w:,} tokens = {100 * tokens / w:.1f}% ({model or 'model unknown'}); "
              f"warn at {c['warn_pct']}%, limit {c['limit_pct']}%")
        return
    root = find_root()
    if not root:
        sys.exit("No .daedalus/config.json here or above. Run `/daedalus:init` (or `dd.py init`) first.")
    cfg = load_config(root)
    if a.cmd in ("resume", "status"):
        print(resume_text(root, cfg, detailed=a.cmd == "status"))
    elif a.cmd == "gate":
        gates = select_gates(cfg, names=a.names or None, include_manual=a.all)
        if not gates:
            sys.exit("No gates selected. Configure them in .daedalus/config.json → gates.")
        results = run_gates(root, cfg, gates, accept=a.accept, reuse=not a.fresh)
        print(format_results(results))
        pending = work_changes(root, cfg)
        if pending:
            print(f"\nRequirement results not updated: {len(pending)} uncommitted file(s). They describe committed "
                  "code, so the commit hook records them (or a gate run on a clean tree).")
        else:
            record_requirements(root, cfg, results)
        for r in results:
            if r.get("tail"):
                print(f"\n[{r['name']}] last lines:\n{r['tail']}")
        sys.exit(1 if any(r["status"] == "fail" for r in results) else 0)
    elif a.cmd == "requirements":
        specs, problems = load_specs(root)
        cov = coverage(root, cfg, specs)
        state = load_json(requirements_path(root), {})
        for s in specs:
            print(f"{s['_file']} · {s.get('milestone', '?')} · {s.get('state', 'active')} · {s.get('title', '')}")
            for r in s.get("requirements") or []:
                st = state.get(r["id"], {})
                c = cov.get(r["id"], {})
                if r.get("verify") == "manual":
                    where = (f"checked by the user on {st.get('at', '')[:10]}" if st.get("passes") else
                             f"the user reported a problem on {st.get('at', '')[:10]}" if st.get("by") else
                             "waits for the user (`dd.py try`)")
                else:
                    where = ", ".join(c.get("tests", []) + c.get("e2e", [])) or "not covered by any test yet"
                print(f"  {'PASS' if st.get('passes') else 'open'}  {r['id']:<14} {r.get('verify', 'test'):<6} {where}")
        for x in problems:
            print("problem:", x)
        sys.exit(1 if problems else 0)
    elif a.cmd == "milestone":
        done, why = milestone_status(root, cfg, a.name)
        print(f"{a.name}: DONE, gate met." if done else f"{a.name}: gate open.\n- " + "\n- ".join(why))
        sys.exit(0 if done else 1)
    elif a.cmd == "run":
        if a.action == "start":
            print(run_start(root, cfg))
        elif a.action == "end":
            if not a.reason.strip():
                sys.exit("Say why the run ends: --reason \"all milestones done\" or what waits on the user.")
            run, lines = run_end(root, cfg, a.reason.strip())
            print(f"Build-all run ended ({OUTCOMES.get(run['outcome'], run['outcome'])}): {run['reason']}\n"
                  + "\n".join(lines))
        else:
            print(run_text(root, cfg))
    elif a.cmd == "change":
        if a.action == "add":
            print(change_from_user(root, " ".join(a.what), a.by))
        elif a.action == "done":
            items = load_json(changes_path(root), [])
            hit = next((c for c in items if a.what and str(c.get("n")) == a.what[0]), None)
            if not hit:
                sys.exit(f"No change {' '.join(a.what)}. `dd.py change` lists them.")
            hit.update(state="planned", where=a.where, planned=dt.datetime.now().isoformat(timespec="minutes"))
            save_json(changes_path(root), items)
            print(f"Change {hit['n']} is in the plan" + (f" ({a.where})." if a.where else "."))
        else:
            items = load_json(changes_path(root), [])
            print("\n".join(f"{c['n']}. [{'open' if c['state'] == 'open' else 'in the plan: ' + (c.get('where') or '?')}] "
                            f"{c['at'].replace('T', ' ')} {c['text']}" for c in items) or "No changes recorded.")
    elif a.cmd == "verify-manual":
        specs, _ = load_specs(root)
        req = next((r for s in specs for r in s.get("requirements") or [] if r.get("id") == a.id), None)
        if not req or req.get("verify") != "manual":
            sys.exit(f"{a.id} isn't a manual requirement in .daedalus/specs/.")
        record_manual(root, a.id, not a.fail, a.note, "user")
        print(f"{a.id}: recorded as {'failing' if a.fail else 'verified'}.")
    elif a.cmd == "try":
        if a.ask:
            try_ask(root, cfg, a.which)
        else:
            print(try_text(root, cfg, a.which))
    elif a.cmd == "setup":
        print("\n".join(setup_hosts(root, cfg)))
    elif a.cmd == "doctor":
        ok, lines = doctor(root, cfg)
        print("\n".join(lines))
        sys.exit(0 if ok else 1)
    elif a.cmd == "review-record":
        review_record(root, cfg)
    elif a.cmd == "review-status":
        files, fp = review_needed(root, cfg)
        why = review_status(root, cfg)
        print("No review needed for the current changes." if not files else
              f"{len(files)} file(s), fingerprint {fp}: " + ("approved." if not why else why))
        sys.exit(1 if why else 0)
    elif a.cmd == "snapshot":
        print(snapshot(root, cfg, a.reason))
    elif a.cmd == "handoff-name":
        slug = re.sub(r"[^a-z0-9]+", "-", a.slug.lower()).strip("-") or "session"
        print(os.path.join(p(root, cfg["state_dir"]), f"{dt.date.today():%Y-%m-%d}-{slug}.md"))


if __name__ == "__main__":
    main()
