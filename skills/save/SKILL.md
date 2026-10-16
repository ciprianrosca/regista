---
name: save
description: "Checkpoint a Daedalus project: run the gates, update the status file, commit locally, and write the session handoff so the next session can resume without this conversation. Use when the user says 'daedalus save', before ending a session, when the Stop hook reports unsaved commits, or when the context is getting long."
---

# Save: leave a clean desk

The Stop hook blocks the end of a turn while commits exist that no handoff covers. This is how to
clear it properly. The CLI path is in the session-start context: `python "${CLAUDE_PLUGIN_ROOT}/scripts/dd.py" …`.

1. **Gates.** `python "${CLAUDE_PLUGIN_ROOT}/scripts/dd.py" gate`. Record pass/fail; never hide a failure. If a metric gate
   improved and the user agrees the new numbers are the new floor, run `gate <name> --accept`.
2. **Status file.** Tick finished plan steps, update "Latest results" (date, numbers, how they were
   measured), and set "Next" to the single next concrete step.
3. **Commit locally** if the gates pass and there are changes. Check `git status` first so nothing
   secret or confidential gets staged. The commit hook runs the gates again; if it blocks, fix the
   cause instead of committing around it. If gates fail, don't commit; say so in the handoff.
   **Never push** unless the user asked (pushes ask for permission anyway).
4. **Handoff.** Get the path with `python "${CLAUDE_PLUGIN_ROOT}/scripts/dd.py" handoff-name "<2–4 word slug>"` and write it
   with the template below. One file per save. Keep it to one screen; history lives in git.
   Write it **after** the session's last commit: the harness compares their times.
5. **Board.** Update `.daedalus/TODO.md`: new decisions, human tasks, the `_Last updated_` line.
6. **Learn.** Run `/daedalus:learn` for anything learned this session.
7. Reply with the resume point in one or two lines and the commit hash (or why there isn't one).

## Handoff template

```markdown
# Daedalus handoff — YYYY-MM-DD (<slug>)

## Done this session
- <what changed, with file paths and commit hashes>

## Latest results
- <date · gate or measurement · numbers> (or "no new run")

## In flight
- <half-finished work, branch, what's left>

## Resume point
<the single next step, concrete enough that a fresh session can start it without asking>

## Blocked on
- <decision or human task, and who>

## Repo state
- <branch> @ <hash>, <clean | uncommitted: files>, <N commits not pushed>
- gates: <pass | fail: which>
```

The handoff and status file are part of the project: they get committed with the next checkpoint.
