---
name: shards
description: "Split the project's test suite over several Claude Code cloud sessions and report how it went: `/regista:shards [ref] [--suite S] [--shards N]`. Started by a person only, because it starts paid sessions."
argument-hint: "[ref, default HEAD] [--suite <name>] [--shards <n>]"
disable-model-invocation: true
---

# Shards: the suite on several cloud sessions

The person asked for a run of the test suite split over cloud sessions. The CLI is
`python "${CLAUDE_PLUGIN_ROOT}/scripts/regista.py"` (its path is also in the session-start context). The project
decides how its suite is split and what each session runs, behind the config's `"shards"` entry; you start the
sessions and follow the run.

0. **Check the tools.** This needs Claude Code's cloud-session tools `create_session` and `send_later`
   (`list_events` helps but is optional); load them if they're deferred. If they aren't there (a local terminal,
   Codex), stop: say this skill runs only in a Claude Code cloud session, and suggest
   `claude --cloud "/regista:shards <ref>"` from a terminal. Start nothing.
1. **Prepare the run:** `python "${CLAUDE_PLUGIN_ROOT}/scripts/regista.py" shards start <ref> [--suite S] [--shards N]`
   with the person's arguments. It checks that the commit is on a remote (the sessions check out from there; never
   push it yourself), caps the number of sessions, runs the project's start command, records the run and prints its
   label, the prompt files and the exact next steps. It starts no session.
   - Exit 75 or "can't prepare a sharded run now": report the reason and stop.
   - Any other failure (no `"shards"` entry, the commit isn't pushed, the project's start failed): report what it
     says, with its fix, and stop. Don't add the config entry yourself: it changes how gates may pass, so it's the
     person's to add.
   - "already runs" or "already passed": report it; go to step 3 for a running one. Only `--fresh` prepares another
     run, and only when the person asked for one.
2. **Start the sessions.** Note the time now, in UTC. Then, in ONE turn, call `create_session` once per prompt file,
   with exactly what step 1 printed:
   - `prompt`: the file's exact text (read it; don't change, shorten or add to it);
   - `source_url`: the https address it printed; `source_revision`: the full commit sha it printed;
   - no `environment_id` (the sessions inherit this one);
   - `model`: the one it printed if the project names one; otherwise the smallest capable current model;
   - `title`: as printed, `<project> shards <label> K/N`.
   If a call fails, report which shards started and which didn't; don't retry in a loop.
3. **Wait without busy-looping.** Schedule `send_later` (first in about 5 minutes, then about every 3). Each time,
   run `python "${CLAUDE_PLUGIN_ROOT}/scripts/regista.py" shards status <label>`: exit 0 is PASSED, 1 is FAILED or
   TIMED OUT, 3 is still running (or couldn't tell), 75 is can't be reached from here. Stop scheduling once it says
   PASSED, FAILED or TIMED OUT. `list_events` on a session shows what it's doing when one seems stuck.
4. **Report**, dated (UTC): the wall time from the time you noted to the status that said it was done, plus any
   timing the project's result line gives; the counts it reports (passed, failed, skipped, shards); each failure
   with its test and cause; and which gates accept the run as their pass (the status says so, or that none does:
   then the run is a result to report, not a gate pass). A running, waiting or failed run is never a pass.
5. **Never** edit code, commit, push or open pull requests in this skill, and don't stop or restart the sessions
   unless the person asks. A failure is reported, not fixed here.
