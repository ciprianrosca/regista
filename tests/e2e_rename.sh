#!/usr/bin/env bash
# End-to-end check for the rename: validate the plugin, set up a fresh project with `init --auto`,
# and drive the session-start, pre-tool and stop hooks through the launcher scripts/regista. The
# project must get a .regista folder and no .daedalus folder, and every answer must say "Regista".
# covers: E2E-0, CODE-9
# Then build an old-style project from tests/fixtures/old-project, check it still works, run
# `migrate --check` and `migrate`, and check the moved project works with the same gates, its
# exclude-file workers block is renamed, its ignored files move with it and a fresh gate run passes.
# covers: MIG-10, E2E-1
# Last, check that this repository mentions the old name only where it is allowed to.
# covers: E2E-2
#
# Run from anywhere: bash tests/e2e_rename.sh
# E2E_SKIP_VALIDATE=1 skips only the `claude plugin validate` check (for shells that can't start
# a nested Claude session); the gate runs without it.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHER="$ROOT/scripts/regista"

TMP_DIRS=()
cleanup() {
  local d
  for d in "${TMP_DIRS[@]+"${TMP_DIRS[@]}"}"; do rm -rf "$d"; done
}
trap cleanup EXIT

new_tmp() {
  local d
  d="$(mktemp -d)"
  TMP_DIRS+=("$d")
  printf '%s\n' "$d"
}

FAILURES=0
ok()   { printf 'ok    %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; [ -n "${2:-}" ] && printf '      %s\n' "$2"; FAILURES=$((FAILURES + 1)); }

# --- expected names -----------------------------------------------------------------------------

# The new names, written out here rather than read back from the tool, so a wrong constant can't
# make the checks agree with it.
DISPLAY_NAME="Regista"
PROJECT_DIR=".regista"
OLD_PROJECT_DIR=".daedalus"
SLUG="regista"
printf 'expected names: display name "%s", project folder "%s"\n' "$DISPLAY_NAME" "$PROJECT_DIR"

# --- helpers ------------------------------------------------------------------------------------

# A home folder of our own for every run of the tool: this machine's own Claude Code settings may
# enable the old plugin, and the warning about that would show up in the hook answers. Only
# `claude plugin validate` keeps the real home.
TEST_HOME="$(mktemp -d)"
TMP_DIRS+=("$TEST_HOME")

# Run the tool through the launcher with CLAUDE_PROJECT_DIR unset and the test home folder.
run_tool() {
  env -u CLAUDE_PROJECT_DIR HOME="$TEST_HOME" CODEX_HOME="$TEST_HOME/.codex" sh "$LAUNCHER" "$@"
}

# Run a hook through the launcher with JSON on stdin.
# Prints stdout; returns the hook's exit code.
run_hook() {
  local event="$1" payload="$2"
  printf '%s' "$payload" | run_tool hook "$event"
}

# Check a hook answer with a Python expression over `out` (the parsed JSON object) and the
# variables `name` and `slug`. Prints a reason on stdout and returns non-zero if it doesn't hold.
check_json() {
  local answer="$1" expr="$2"
  ANSWER="$answer" NAME="$DISPLAY_NAME" SLUG="$SLUG" python3 - "$expr" <<'PY'
import json, os, sys
raw = os.environ["ANSWER"]
try:
    out = json.loads(raw)
except ValueError:
    print("answer is not one JSON object: %r" % raw[:300])
    sys.exit(1)
if not isinstance(out, dict):
    print("answer is not a JSON object: %r" % raw[:300])
    sys.exit(1)
name, slug = os.environ["NAME"], os.environ["SLUG"]
hso = out.get("hookSpecificOutput") or {}
if not eval(sys.argv[1]):
    print("unexpected answer: %s" % raw[:500])
    sys.exit(1)
PY
}

# Run a hook and check its answer; one ok/FAIL line.
hook_check() {
  local label="$1" event="$2" payload="$3" expr="$4" answer why
  if ! answer="$(run_hook "$event" "$payload" 2>&1)"; then
    fail "$label" "hook exited non-zero: ${answer:0:500}"
    return
  fi
  if why="$(check_json "$answer" "$expr")"; then ok "$label"; else fail "$label" "$why"; fi
}

json_str() { python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$1"; }

# --- checks -------------------------------------------------------------------------------------

check_plugin_validates() {
  if [ "${E2E_SKIP_VALIDATE:-}" = "1" ]; then
    printf 'skip  claude plugin validate (E2E_SKIP_VALIDATE=1)\n'
    return
  fi
  local out
  if out="$(claude plugin validate "$ROOT" 2>&1)"; then
    ok "claude plugin validate"
  else
    fail "claude plugin validate" "${out:0:500}"
  fi
}

PROJECT=""
check_init_auto() {
  PROJECT="$(new_tmp)"
  (
    cd "$PROJECT"
    git init -q
    git config user.name "E2E Test"
    git config user.email "e2e@example.invalid"
    printf 'print("hello")\n' > app.py
    git add app.py
    git commit -q -m "first commit"
  )
  local out
  if ! out="$(cd "$PROJECT" && run_tool init --auto 2>&1)"; then
    fail "init --auto exits 0" "${out:0:500}"
    return
  fi
  if [ -f "$PROJECT/$PROJECT_DIR/config.json" ]; then
    ok "init --auto creates $PROJECT_DIR/config.json"
  else
    fail "init --auto creates $PROJECT_DIR/config.json" "missing; init said: ${out:0:300}"
  fi
  local old
  old="$(find "$PROJECT" -name "$OLD_PROJECT_DIR" -type d -print)"
  if [ -z "$old" ]; then
    ok "init --auto creates no $OLD_PROJECT_DIR folder"
  else
    fail "init --auto creates no $OLD_PROJECT_DIR folder" "found: ${old:0:300}"
  fi
}

check_session_start() {
  hook_check "session-start names the harness \"$DISPLAY_NAME\"" session-start \
    "{\"cwd\": $(json_str "$PROJECT"), \"source\": \"startup\"}" \
    'hso.get("hookEventName") == "SessionStart" and "runs on the Regista harness" in (hso.get("additionalContext") or "") and "Daedalus" not in raw'
}

check_pre_tool_denies_env() {
  hook_check "pre-tool denies writing .env, reason starts \"$DISPLAY_NAME: \"" pre-tool \
    "{\"cwd\": $(json_str "$PROJECT"), \"tool_name\": \"Write\", \"tool_input\": {\"file_path\": $(json_str "$PROJECT/.env"), \"content\": \"x\"}}" \
    'hso.get("permissionDecision") == "deny" and (hso.get("permissionDecisionReason") or "").startswith("Regista: ") and "Daedalus" not in raw'
}

check_pre_tool_asks_push() {
  hook_check "pre-tool asks before git push, reason starts \"$DISPLAY_NAME: \"" pre-tool \
    "{\"cwd\": $(json_str "$PROJECT"), \"tool_name\": \"Bash\", \"tool_input\": {\"command\": \"git push\"}}" \
    'hso.get("permissionDecision") == "ask" and (hso.get("permissionDecisionReason") or "").startswith("Regista: ") and "Daedalus" not in raw'
}

check_stop_blocks_without_handoff() {
  hook_check "stop blocks without a handoff and points to /$SLUG:save" stop \
    "{\"cwd\": $(json_str "$PROJECT")}" \
    'out.get("decision") == "block" and (out.get("reason") or "").startswith("Regista: ") and "/regista:save" in (out.get("reason") or "") and "Daedalus" not in raw'
}

# --- run ----------------------------------------------------------------------------------------

check_plugin_validates
check_init_auto
if [ -n "$PROJECT" ] && [ -f "$PROJECT/$PROJECT_DIR/config.json" ]; then
  check_session_start
  check_pre_tool_denies_env
  check_pre_tool_asks_push
  check_stop_blocks_without_handoff
else
  fail "hook checks" "skipped because init --auto did not set up the project"
fi

# --- manifests name the plugin regista 0.9.0 ----------------------------------------------------
# covers: DOC-6
# The expected values are literals, not the constants block in scripts/regista.py.

MANIFEST_NAME="regista"
MANIFEST_VERSION="0.9.0"

# Parse one manifest with python3 and print the reason on stdout if it doesn't name the plugin
# $MANIFEST_NAME at version $MANIFEST_VERSION. For marketplace.json, check its own name and its
# plugin entry's name and version.
check_manifest_json() {
  local path="$1"
  python3 - "$path" "$MANIFEST_NAME" "$MANIFEST_VERSION" <<'PY'
import json, os, sys
path, name, version = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path, "rb") as f:
        data = json.loads(f.read().decode("utf-8"))
except (OSError, ValueError) as e:
    print("cannot read %s: %s" % (path, e))
    sys.exit(1)
if os.path.basename(path) == "marketplace.json":
    plugins = data.get("plugins") or []
    entry = plugins[0] if len(plugins) == 1 and isinstance(plugins[0], dict) else {}
    found = (data.get("name"), entry.get("name"), entry.get("version"), len(plugins))
    if found != (name, name, version, 1):
        print("marketplace name %r, plugin entries %d, entry name %r, entry version %r"
              % (data.get("name"), len(plugins), entry.get("name"), entry.get("version")))
        sys.exit(1)
else:
    if (data.get("name"), data.get("version")) != (name, version):
        print("name %r, version %r" % (data.get("name"), data.get("version")))
        sys.exit(1)
PY
}

check_manifests_name_regista() {
  local rel why
  for rel in .claude-plugin/plugin.json .claude-plugin/marketplace.json .codex-plugin/plugin.json; do
    if why="$(check_manifest_json "$ROOT/$rel")"; then
      ok "$rel names the plugin \"$MANIFEST_NAME\" at version $MANIFEST_VERSION"
    else
      fail "$rel names the plugin \"$MANIFEST_NAME\" at version $MANIFEST_VERSION" "$why"
    fi
  done
}

check_manifests_name_regista

# --- an old project keeps working, and migrate moves it -------------------------------------------
# covers: MIG-10
# A copy of tests/fixtures/old-project (a project set up by Daedalus 0.8, shaped like a real one: a
# .daedalus folder with config, plan, specs, gates, a handoff, a review, memory and a fleet record
# with one removed worker) becomes a git repository in a temporary folder. The checks run there
# only: never in this repository's own .daedalus folder.

OLD_FIXTURE="$ROOT/tests/fixtures/old-project"
MOVE_MESSAGE="chore: move the harness folder to .regista"
OLD_EXCLUDE_HEADER="# Daedalus workers"
NEW_EXCLUDE_HEADER="# Regista workers"
OLD_PROJECT=""

# Git variables a calling git command may have set would point every git call at another repository.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX GIT_COMMON_DIR

# Harness variables from the session that runs this script (a worker's name and slot, under the new
# or the old name) would make the tool act as that worker; the old project must see none of them.
HARNESS_VARS=()
for v in $(compgen -e); do
  case "$v" in REGISTA_*|DAEDALUS_*) HARNESS_VARS+=(-u "$v") ;; esac
done

# Run the tool in the old project, like run_tool but without any harness variable.
run_old() {
  (cd "$OLD_PROJECT" && env "${HARNESS_VARS[@]+"${HARNESS_VARS[@]}"}" -u CLAUDE_PROJECT_DIR \
    HOME="$TEST_HOME" CODEX_HOME="$TEST_HOME/.codex" sh "$LAUNCHER" "$@")
}

# Session start in the old project; prints the answer.
old_session_start() {
  printf '%s' "{\"cwd\": $(json_str "$OLD_PROJECT"), \"source\": \"startup\"}" | run_old hook session-start
}

# Check a session-start answer names Regista and has `want` lines (0 or 1) saying the project still
# uses the old folder: a line naming .daedalus and /regista:start. This repository's own path is
# taken out first, because while it is being built it sits under a .daedalus folder.
check_start_answer() {
  local answer="$1" want="$2"
  ANSWER="$answer" HARNESS="$ROOT" python3 - "$want" <<'PY'
import json, os, sys
raw = os.environ["ANSWER"]
try:
    out = json.loads(raw)
except ValueError:
    print("answer is not one JSON object: %r" % raw[:300])
    sys.exit(1)
ctx = ((out.get("hookSpecificOutput") or {}).get("additionalContext") or "") if isinstance(out, dict) else ""
if "runs on the Regista harness" not in ctx:
    print("the answer doesn't say \"runs on the Regista harness\": %r" % raw[:500])
    sys.exit(1)
text = ctx.replace(os.environ["HARNESS"], "<harness>")
lines = [ln for ln in text.splitlines() if ".daedalus" in ln and "/regista:start" in ln]
if len(lines) != int(sys.argv[1]):
    print("expected %s line(s) about the old folder, found %d: %r" % (sys.argv[1], len(lines), lines))
    sys.exit(1)
PY
}

# Folders called $1 in the old project, not looking inside .git.
old_folders_named() {
  find "$OLD_PROJECT" -path "$OLD_PROJECT/.git" -prune -o -type d -name "$1" -print
}

# Everything migrate --check could change: git's view (status with ignored files, HEAD, the index,
# the branches and the exclude file) and every file and folder on disk with a checksum.
tree_state() {
  (
    cd "$OLD_PROJECT"
    printf '== status\n'; git status --porcelain --ignored
    printf '== head\n'; git rev-parse HEAD
    printf '== index\n'; git ls-files -s
    printf '== refs\n'; git for-each-ref
    printf '== exclude\n'; cat .git/info/exclude 2>/dev/null || true
    printf '== folders\n'; find . -path ./.git -prune -o -type d -print | LC_ALL=C sort
    printf '== files\n'
    find . -path ./.git -prune -o -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum
  )
}

setup_old_project() {
  OLD_PROJECT="$(new_tmp)"
  cp -R "$OLD_FIXTURE/." "$OLD_PROJECT/"
  # The fixture keeps its two .gitignore files under the plain name "gitignore": stored under their
  # real name they would apply to this repository and hide fixture files (the harness folder's one
  # ignores fleet/). They get their real name in the copy.
  mv "$OLD_PROJECT/gitignore" "$OLD_PROJECT/.gitignore"
  mv "$OLD_PROJECT/$OLD_PROJECT_DIR/gitignore" "$OLD_PROJECT/$OLD_PROJECT_DIR/.gitignore"
  local out
  if ! out="$(
    cd "$OLD_PROJECT" &&
    git init -q -b main &&
    git config user.name "E2E Test" &&      # migrate makes its own commit, with no home git config
    git config user.email "e2e@example.invalid" &&
    git add -A &&
    git -c user.name=t -c user.email=t@t commit -q -m "first commit" 2>&1
  )"; then
    fail "old project: the fixture becomes a git repository" "${out:0:500}"
    OLD_PROJECT=""
    return
  fi
  # covers: E2E-1
  # The block a Daedalus 0.8 overseer writes into .git/info/exclude for its workers. It goes in
  # before any "before" state is taken, so migrate --check is also checked to leave it alone.
  mkdir -p "$OLD_PROJECT/.git/info"
  printf '%s\n' "$OLD_EXCLUDE_HEADER" "$OLD_PROJECT_DIR/worktrees/" "$OLD_PROJECT_DIR/fleet/" \
    "$OLD_PROJECT_DIR/worker.json" >> "$OLD_PROJECT/.git/info/exclude"
  local dirty
  dirty="$(cd "$OLD_PROJECT" && git status --porcelain)"
  if [ -z "$dirty" ] && [ -f "$OLD_PROJECT/$OLD_PROJECT_DIR/config.json" ] \
     && [ -z "$(old_folders_named "$PROJECT_DIR")" ]; then
    ok "old project: the fixture becomes a git repository with one commit and a clean tree"
  else
    fail "old project: the fixture becomes a git repository with one commit and a clean tree" "status: ${dirty:0:300}"
    OLD_PROJECT=""
  fi
}

check_old_project_works() {
  local answer why out
  if answer="$(old_session_start 2>&1)" && why="$(check_start_answer "$answer" 1)"; then
    ok "old project: session start names \"$DISPLAY_NAME\" and says once it still uses $OLD_PROJECT_DIR"
  else
    fail "old project: session start names \"$DISPLAY_NAME\" and says once it still uses $OLD_PROJECT_DIR" \
      "${why:-${answer:0:500}}"
  fi
  if out="$(run_old gate 2>&1)"; then
    ok "old project: regista.py gate runs the fixture's gates and they pass"
  else
    fail "old project: regista.py gate runs the fixture's gates and they pass" "${out:0:500}"
  fi
  local found
  found="$(old_folders_named "$PROJECT_DIR")"
  if [ -z "$found" ] && [ -f "$OLD_PROJECT/$OLD_PROJECT_DIR/config.json" ]; then
    ok "old project: no $PROJECT_DIR folder appears before the move"
  else
    fail "old project: no $PROJECT_DIR folder appears before the move" "found: ${found:0:300}"
  fi
}

check_migrate_check() {
  local before after out
  before="$(tree_state)"
  if ! out="$(run_old migrate --check 2>&1)"; then
    fail "migrate --check exits 0 in the old project" "${out:0:800}"
  else
    local missing="" name
    for name in "$OLD_PROJECT_DIR" "$PROJECT_DIR" config.json plan.md AGENTS.md .gitignore settings.json \
                daedalus-builder.toml MEMORY.md; do
      case "$out" in *"$name"*) ;; *) missing="$missing $name" ;; esac
    done
    if [ -z "$missing" ]; then
      ok "migrate --check lists the move and the files it would change"
    else
      fail "migrate --check lists the move and the files it would change" "doesn't mention:$missing; said: ${out:0:800}"
    fi
  fi
  after="$(tree_state)"
  if [ "$before" = "$after" ]; then
    ok "migrate --check changes nothing (git status, HEAD, index and every file's checksum)"
  else
    fail "migrate --check changes nothing (git status, HEAD, index and every file's checksum)" \
      "$(diff <(printf '%s\n' "$before") <(printf '%s\n' "$after") | head -20 | tr '\n' ' ')"
  fi
}

MIGRATED=0
check_migrate() {
  local head_before count_before out
  head_before="$(cd "$OLD_PROJECT" && git rev-parse HEAD)"
  count_before="$(cd "$OLD_PROJECT" && git rev-list --count HEAD)"
  if ! out="$(run_old migrate 2>&1)"; then
    fail "migrate exits 0 in the old project" "${out:0:800}"
    return
  fi
  ok "migrate exits 0 in the old project"
  local count parent subject status
  count="$(cd "$OLD_PROJECT" && git rev-list --count HEAD)"
  parent="$(cd "$OLD_PROJECT" && git rev-parse -q --verify HEAD~1 || true)"
  subject="$(cd "$OLD_PROJECT" && git log -1 --format=%s)"
  status="$(cd "$OLD_PROJECT" && git status --porcelain)"
  if [ "$count" -eq $((count_before + 1)) ] && [ "$parent" = "$head_before" ] \
     && [ "$subject" = "$MOVE_MESSAGE" ] && [ -z "$status" ]; then
    ok "migrate makes one commit \"$MOVE_MESSAGE\" and leaves a clean tree"
  else
    fail "migrate makes one commit \"$MOVE_MESSAGE\" and leaves a clean tree" \
      "commits $count_before -> $count, parent ok: $([ "$parent" = "$head_before" ] && echo yes || echo no), subject: $subject, status: ${status:0:300}"
  fi
  local old
  old="$(old_folders_named "$OLD_PROJECT_DIR")"
  if [ -z "$old" ] && [ -f "$OLD_PROJECT/$PROJECT_DIR/config.json" ]; then
    MIGRATED=1
    ok "migrate leaves $PROJECT_DIR/config.json and no $OLD_PROJECT_DIR folder"
  else
    fail "migrate leaves $PROJECT_DIR/config.json and no $OLD_PROJECT_DIR folder" "found: ${old:0:300}"
  fi
}

# covers: E2E-1
# Every file git ignores under the old folder, with its checksum, just before migrate runs
# (`checksum  path` lines, paths from the project root).
IGNORED_BEFORE=""
snapshot_ignored_files() {
  IGNORED_BEFORE="$(cd "$OLD_PROJECT" &&
    git ls-files -z --others --ignored --exclude-standard -- "$OLD_PROJECT_DIR" |
    LC_ALL=C sort -z | xargs -0 -r sha256sum)"
}

# covers: E2E-1
# Right after migrate, before anything else runs there: each ignored file is at the same path under
# the new folder with the same checksum. Fails if there was no ignored file to follow.
check_ignored_files_moved() {
  local label="migrate moves every ignored file in $OLD_PROJECT_DIR to the same path in $PROJECT_DIR, unchanged"
  local why
  if [ -z "$IGNORED_BEFORE" ]; then
    fail "$label" "git ignored no file under $OLD_PROJECT_DIR before the move, so there was nothing to check"
    return
  fi
  if why="$(cd "$OLD_PROJECT" && BEFORE="$IGNORED_BEFORE" python3 - "$OLD_PROJECT_DIR" "$PROJECT_DIR" <<'PY'
import hashlib, os, sys
old, new = sys.argv[1] + "/", sys.argv[2] + "/"
bad, paths = [], []
for line in os.environ["BEFORE"].splitlines():
    digest, path = line[:64], line[66:]
    paths.append(path)
    if not path.startswith(old):
        bad.append("%s is not under %s" % (path, old))
        continue
    moved = new + path[len(old):]
    try:
        with open(moved, "rb") as f:
            got = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        bad.append("%s is missing (was %s)" % (moved, path))
        continue
    if got != digest:
        bad.append("%s has a different checksum from %s" % (moved, path))
if old + "fleet/workers.json" not in paths:
    bad.append("the fixture's fleet/workers.json was not among the ignored files: %r" % paths[:10])
if bad:
    print("; ".join(bad[:10]))
    sys.exit(1)
print(len(paths))
PY
  )"; then
    ok "$label ($why file(s))"
  else
    fail "$label" "$why"
  fi
}

# covers: E2E-1
# After migrate the exclude file has the "# Regista workers" block naming the three new paths, and
# no line names the old folder or keeps the old header.
check_exclude_block_renamed() {
  local label="migrate replaces the exclude file's \"$OLD_EXCLUDE_HEADER\" block with \"$NEW_EXCLUDE_HEADER\""
  local why
  if why="$(python3 - "$OLD_PROJECT/.git/info/exclude" "$NEW_EXCLUDE_HEADER" "$OLD_EXCLUDE_HEADER" \
             "$PROJECT_DIR" "$OLD_PROJECT_DIR" <<'PY'
import sys
path, header, old_header, new, old = sys.argv[1:6]
try:
    with open(path, encoding="utf-8") as f:
        lines = [ln.rstrip("\n") for ln in f]
except OSError as e:
    print("cannot read the exclude file: %s" % e)
    sys.exit(1)
problems = []
if header not in lines:
    problems.append("no %r line" % header)
else:
    block = []
    for ln in lines[lines.index(header) + 1:]:
        if not ln.strip() or ln.startswith("#"):
            break
        block.append(ln.strip())
    want = [new + "/worktrees/", new + "/fleet/", new + "/worker.json"]
    missing = [w for w in want if w not in block]
    if missing:
        problems.append("the block after %r lacks %s (block: %r)" % (header, missing, block))
stale = [ln for ln in lines if old + "/" in ln or ln.strip() == old_header]
if stale:
    problems.append("lines still naming the old folder or header: %r" % stale)
if problems:
    print("; ".join(problems) + "; last lines: %r" % lines[-12:])
    sys.exit(1)
PY
  )"; then
    ok "$label"
  else
    fail "$label" "$why"
  fi
}

check_moved_project_works() {
  local answer why out
  if answer="$(old_session_start 2>&1)" && why="$(check_start_answer "$answer" 0)"; then
    ok "after the move: session start names \"$DISPLAY_NAME\" and has no old-folder line"
  else
    fail "after the move: session start names \"$DISPLAY_NAME\" and has no old-folder line" "${why:-${answer:0:500}}"
  fi
  if out="$(run_old gate 2>&1)"; then
    ok "after the move: regista.py gate runs the gates and they pass"
  else
    fail "after the move: regista.py gate runs the gates and they pass" "${out:0:500}"
  fi
  # covers: E2E-1
  if out="$(run_old gate --fresh 2>&1)"; then
    ok "after the move: regista.py gate --fresh runs every gate again and they pass"
  else
    fail "after the move: regista.py gate --fresh runs every gate again and they pass" "${out:0:500}"
  fi
  if why="$(python3 - "$OLD_FIXTURE/$OLD_PROJECT_DIR/config.json" "$OLD_PROJECT/$PROJECT_DIR/config.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    want = json.load(f)["gates"]
with open(sys.argv[2], encoding="utf-8") as f:
    got = json.load(f).get("gates")
if json.dumps(got) != json.dumps(want):
    print("fixture gates %s, moved gates %s" % (json.dumps(want)[:300], json.dumps(got)[:300]))
    sys.exit(1)
PY
  )"; then
    ok "after the move: $PROJECT_DIR/config.json has the fixture's gates, unchanged and in order"
  else
    fail "after the move: $PROJECT_DIR/config.json has the fixture's gates, unchanged and in order" "$why"
  fi
  if out="$(run_old migrate --check 2>&1)" && printf '%s' "$out" | grep -qi "nothing to do"; then
    ok "after the move: migrate --check says there's nothing to do"
  else
    fail "after the move: migrate --check says there's nothing to do" "${out:0:500}"
  fi
}

setup_old_project
if [ -n "$OLD_PROJECT" ]; then
  check_old_project_works
  check_migrate_check
  snapshot_ignored_files
  check_migrate
  if [ "$MIGRATED" -eq 1 ]; then
    check_ignored_files_moved
    check_exclude_block_renamed
    check_moved_project_works
  else
    fail "checks after the move" "skipped because migrate did not move the project"
  fi
else
  fail "migration checks" "skipped because the old project could not be set up"
fi

# --- the old name is left only where it belongs ----------------------------------------------------
# covers: E2E-2
# `git grep -il --untracked daedalus` in this repository (ignored files left out, as git grep does by
# default), outside the top-level .daedalus folder, may list only: tests/ (the old-name and migration
# tests, this script and the old-project fixture); README.md, where only one line among the first 15
# (the one saying it was called Daedalus) and the section headed "## Moving a project from Daedalus"
# (up to the next "## " heading) may mention it; and scripts/regista.py, where only the constants
# block may mention it, from the line starting "DISPLAY_NAME = " to the line starting
# "OLD_GATE_LOCK = ", both included. Reads this repository only; changes nothing.

README_SECTION="## Moving a project from Daedalus"
CONSTANTS_FIRST="DISPLAY_NAME = "
CONSTANTS_LAST="OLD_GATE_LOCK = "

check_leftover_names() {
  local label="the old name appears only in tests/, the README's allowed lines and the constants block"
  local listing rc=0 why
  listing="$(git -C "$ROOT" grep -il --untracked daedalus -- . ':(top,exclude).daedalus/' 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    fail "$label" "git grep could not search the repository or found nothing (exit $rc): ${listing:0:300}"
    return
  fi
  if why="$(cd "$ROOT" && LISTING="$listing" python3 - "$README_SECTION" "$CONSTANTS_FIRST" "$CONSTANTS_LAST" <<'PY'
import os, sys
section, first, last = sys.argv[1:4]
files = [p for p in os.environ["LISTING"].splitlines() if p]
problems = []

def mentions(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    return lines, [i for i, ln in enumerate(lines) if "daedalus" in ln.lower()]

def show(path, lines, bad):
    return "%s: %s" % (path, "; ".join("line %d: %s" % (i + 1, lines[i].strip()[:120]) for i in bad[:3]))

for need in ("README.md", "scripts/regista.py"):
    if need not in files:
        problems.append("%s is not in the list, so the search can't be trusted (list: %r)" % (need, files[:10]))

for path in files:
    if path.startswith("tests/"):
        continue
    try:
        lines, hits = mentions(path)
    except OSError as e:
        problems.append("%s (%s)" % (path, e))
        continue
    if path == "README.md":
        allowed = set()
        top = [i for i in hits if i < 15]
        if top:
            allowed.add(top[0])
        start = next((i for i, ln in enumerate(lines) if ln.startswith(section)), None)
        if start is not None:
            end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
            allowed.update(range(start, end))
    elif path == "scripts/regista.py":
        allowed = set()
        start = next((i for i, ln in enumerate(lines) if ln.startswith(first)), None)
        end = None
        if start is not None:
            end = next((j for j in range(start, len(lines)) if lines[j].startswith(last)), None)
        if start is None or end is None:
            problems.append("%s: the constants block (%r to %r) was not found" % (path, first.strip(), last.strip()))
        else:
            allowed = set(range(start, end + 1))
    else:
        problems.append(show(path, lines, hits) if hits else "%s (a binary or unreadable match)" % path)
        continue
    bad = [i for i in hits if i not in allowed]
    if bad:
        problems.append(show(path, lines, bad))

if problems:
    print("\n      ".join(problems))
    sys.exit(1)
# On a pass, list every file that matched, so a reviewer can see what is allowed under tests/.
print("\n".join("      allowed: %s" % p for p in files))
PY
  )"; then
    ok "$label"
    printf '%s\n' "$why"
  else
    fail "$label" "$why"
  fi
}

check_leftover_names

if [ "$FAILURES" -eq 0 ]; then
  printf 'all end-to-end checks passed\n'
  exit 0
fi
printf '%d end-to-end check(s) failed\n' "$FAILURES"
exit 1
