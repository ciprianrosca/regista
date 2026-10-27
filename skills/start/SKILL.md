---
name: start
description: "Resume a Regista project: where we are, what's next, what's blocked. Also 'status' (full report) and 'todo' (the board). Use at the start of a session, when the user says 'regista start', 'regista status', 'regista todo' or 'where are we', or after a context compaction."
---

# Start: resume, status, todo

The session-start hook already loaded the latest state into context, including the Regista CLI
path (`python "${CLAUDE_PLUGIN_ROOT}/scripts/regista.py" …`). This skill checks that state and fills the gaps.

## `/regista:start`
1. Run `python "${CLAUDE_PLUGIN_ROOT}/scripts/regista.py" resume`.
2. Read `.regista/memory/MEMORY.md`, the newest handoff in full, and the plan's current milestone.
3. **Trust git over notes.** If the handoff says "uncommitted" but the tree is clean (or the other way
   round), or a number in the handoff disagrees with the status file, say so. The newer dated source
   wins; fix the other at the next save.
4. If a newer automatic snapshot (`-auto-` in the name) exists, the last session ended without
   saving: read it, look at the commits it lists, and reconstruct the resume point first.
5. Reply in three lines:
   - **Where we are:** milestone + last completed step + latest gate results (dated)
   - **Next:** the resume point
   - **Blocked on:** open decisions or human tasks that stop the next step
6. **`/regista:start continue`** (the resume prompt after a pause): don't wait. If the handoff
   has work "In flight", finish that first; otherwise carry on from the resume point with the build
   loop (`/regista:build`). Stop only if the next step is blocked. If a build-all run is active (the
   state says so; `regista.py run` shows it), carry on with `/regista:build all`.
   Without `continue`, wait for the user's command.

**A project on the old folder.** When the session-start context says the project still uses the old
folder, everything keeps working there (read the files above in that folder instead of `.regista/`).
After the three lines, offer the move: show the user what
`python "${CLAUDE_PLUGIN_ROOT}/scripts/regista.py" migrate --check` lists, and run
`python "${CLAUDE_PLUGIN_ROOT}/scripts/regista.py" migrate` only after they say yes.

## `/regista:status`
`python "${CLAUDE_PLUGIN_ROOT}/scripts/regista.py" status`, then a compact report: milestones (done / in progress / not started, with
the current one's open steps), the last gate run, open decisions oldest first and who they wait on,
and git (branch, uncommitted files, commits not pushed).

## `/regista:todo`
Show the board's open items grouped by section. Don't edit it unless asked.
