---
name: pause
description: "Pause gracefully when the context budget runs out: bring the work to a safe point, save everything (gates, commit if green, status, handoff with a resume prompt, board), and hand the user the exact steps to clear and resume. Use when a Daedalus hook says the context is over its limit, when the user says 'daedalus pause', or before the context gets long."
---

# Pause: stop cleanly, resume in a fresh context

Normally you don't need this: by default (`context.on_limit: "compact"`) the session compacts itself
at the limit (default 65%) and the work carries on from the saved state. Pausing is for when that
can't happen: the project chose `on_limit: "pause"`, or the context passed the limit and compaction
didn't fire (the hook says so; `dd.py doctor` finds the cause). Then the hooks refuse every tool call
except saving: bookkeeping files (`.daedalus/`, the status file, the board), git and the Daedalus CLI.
The user can also ask for a pause to stop for the day.

The CLI path is in the session-start context: `python "${CLAUDE_PLUGIN_ROOT}/scripts/dd.py" …`.

1. **Stop new work.** No new edits to code, no new reads of source files, no subagents.
2. **Reach a safe point.**
   - Run `python "${CLAUDE_PLUGIN_ROOT}/scripts/dd.py" gate`.
   - Gates pass and there are changes: commit them (`git add -A && git commit -m "<type>: <what>"`).
     The commit hook runs the gates again.
   - Gates fail, or the step is half done: **don't commit**. Leave the files as they are and describe
     exactly what's unfinished in the handoff's "In flight": files, what works, what's left, and how
     to verify it. Never commit a failing state to get a clean tree.
3. **Status file:** tick finished steps, update "Latest results", and set "Next".
4. **Handoff:** `python "${CLAUDE_PLUGIN_ROOT}/scripts/dd.py" handoff-name "pause <topic>"`, then write it with the save
   template (`/daedalus:save`), plus this section at the end:

   ```markdown
   ## Resume prompt
   /daedalus:start continue
   ```

   Write it after the last commit: the Stop hook allows the turn to end only once a handoff newer
   than the moment the limit was hit exists.
5. **Board:** update `.daedalus/TODO.md` if anything changed.
6. **Commit the bookkeeping** if you committed work in step 2 and the tree is otherwise clean:
   `git add .daedalus <status file> && git commit -m "docs: pause handoff"`.
7. **End your reply with exactly this** (fill in the numbers):

   > Context is at <N>%, over the <limit>% limit, so I've paused. Everything is saved: <commit hash
   > or "uncommitted: files">, handoff `<path>`.
   > **Run `/clear`, then send: `/daedalus:start continue`**

After `/clear`, the session-start hook loads the saved state into the fresh context, and
`/daedalus:start continue` picks up from the resume point.
