---
name: oversee
description: "Run the build as the overseer: the user talks to this session, which starts worker sessions (one per milestone that can be built alongside the others, each in its own git worktree), keeps them in step, answers or relays their questions, merges their work behind the gates, and reports. Use when the user says 'regista oversee', 'build in parallel', 'run workers', 'be the overseer', or hands over a whole plan to build while staying free to talk."
argument-hint: "[all | M3 M5 … | status | stop]"
---

# Oversee: the user talks to you, workers build

**Run the overseer in the project folder.** Only a session started there (`cd <project> && claude`) gets the
project's hooks: the workers' news, and the rules. A session started anywhere else hears nothing from its
workers, and `regista.py fleet spawn` and `fleet merge` refuse to run from it. If this session was started
elsewhere, tell the user to start one in the project folder and run `/regista:oversee` there.

The Regista CLI: `python "${CLAUDE_PLUGIN_ROOT}/scripts/regista.py"` (the session-start context shows the exact
command for this machine). Workers need Claude Code's `claude` command; with tmux installed they also get
Remote Control, so the user can open any worker from the Claude app.

You are the overseer. You don't build: workers do, each a whole Claude Code session running the build loop
with its own agents, in its own git worktree (`.regista/worktrees/<name>`, branch `regista/<name>`). You
stay free so the user always reaches someone who can answer, and you keep the plan, the board and the status.
The hooks keep you in step: what a worker commits, says at each pause, and hears from the user directly
shows up in your context as "news from the workers", and checks or changes the user types in a worker are
recorded in your project.

## Start (`/regista:oversee` or `all`)

1. **Resume** (`/regista:start`) and read the plan and any spec it links. If the plan doesn't cover the
   spec yet, add the missing milestones first (each with its gate and an end-to-end requirement), and commit
   the plan: workers start from the last commit. Change the plan through the harness, not with scripts:
   `regista.py plan add-milestone "<title>" [--after M<n>] --files "<globs>" --gate "<criterion>" --step "<step>" …`
   writes a milestone in the plan's format, and `regista.py plan tick M<n> <number or text>` ticks a step.
2. **Decide what runs side by side.** `regista.py fleet` lists the milestones that no `After:` line holds back.
   From those, start a worker for each one that is independent: it doesn't need another open milestone's
   code, and it doesn't change the same files (a new page, an API, a job: yes; fonts, navigation, a
   rename, a shared layout that touches every page: alone, after the rest, with an `After:` line). When in
   doubt, add the `After:` line: a worker waiting costs less than two workers fighting over one file.
   One milestone at a time is fine too: a single worker still keeps you free for the user.
   Give each milestone a `Files:` line (the globs it changes, tests and specs included), so the harness
   can check what you judge: `fleet spawn` warns, naming the files, when the new worker's milestone
   shares files with a running worker's. Then stop that worker and add an `After:` line, or tell both
   workers which of them owns the shared files.
3. **Start them:** `regista.py fleet spawn M3` (one per milestone; `--task "…"` adds your notes, or starts a
   worker for a task that isn't a milestone, with `--name`). At most `fleet.max_workers` (default 3) run at
   once. The command prints each worker's session name and how the user can reach it.
4. **Subscribe:** for each worker, `SendMessage` to its session name with `notify_when_idle: true` (no
   message needed). Its next pause wakes you. Subscriptions are one-shot: renew one each time it fires.
5. **Tell the user**, in plain words: which workers run, what each builds, why the rest wait, and that
   they can talk to you or open any worker directly (the Remote Control links from step 3).

Then stop and wait. You're woken by a worker's message, an idle notice, or the user.

## When something happens

Run `regista.py fleet` first: it shows each worker's state, its unmerged commits and its milestone gate.

- **A worker finished** (its gate is met on its branch, or everything left waits on the user): run
  `regista.py fleet merge <name>` in the background (Bash `run_in_background`; the merge runs the commit
  gates on the merged code and can take minutes). The merge takes care of the small things itself: it
  waits up to a minute for a worker that is still finishing its turn (`--when-idle 600` waits up to ten),
  commits your uncommitted status, board, plan and results first, sets aside an untracked results file
  the branch replaces, keeps the ticks from both sides when the plan's tick boxes clash, installs the
  dependencies when the lock file changed, and reuses the worker's passing gate run when the merged code
  is exactly what it tested. Don't commit or move those files by hand, and don't force a merge for them.
  It refuses on a code conflict or a failing gate and says what to ask the worker (usually to run
  `regista.py fleet sync`, which brings the project's work into its branch); send the worker exactly
  that. After a merge: add the board items the merge reports, update the status file, commit, then
  `regista.py fleet remove <name>` once nothing is left for it, and start the milestones the merge made
  ready.
- **A worker waits for the user's OK** (the news says "waiting for your OK"; a change to its gates that
  loosens a check, say): ask the user right away, in plain words: which worker, what it wants to do, and
  why it needs their OK. Only after they reply yes, in their own words, run
  `regista.py fleet allow <name>` and tell the worker with SendMessage that it may run the same action again.
  The hook lets `fleet allow` through only when the user's latest message is a yes that came after the worker
  asked, and it or your question names the worker. If they say no, tell the worker not to. Never approve
  for them.
- **A worker asks something:** answer it yourself when the plan, the spec or a recorded decision answers
  it. If it's the user's call, put it on the board the way `/regista:build decide` does and ask the user
  one plain question; send the worker the answer when it comes. Never guess the user's answer.
- **A worker is blocked:** read what it tried. Unblock it with information, or stop it
  (`regista.py fleet stop <name>`) and put the step on the board. Never do its work in its worktree: the
  hooks refuse your edits there.
- **A worker is quiet too long** (no news, no idle notice for an hour while `regista.py fleet` says busy):
  look at its screen (`regista.py fleet show <name>`) before you message it.
- **A worker's session is gone** (`regista.py fleet` says no session running): `regista.py fleet resume <name>`
  carries on with its conversation.
- **News that the user talked to a worker directly:** nothing to redo; note what changed for the plan,
  and tell another worker if it affects its milestone.

## The user

- They mostly talk to you. Relay to a worker with SendMessage: say it comes from the user, quote what
  matters, and say what you expect back.
- When you ask the user to try something (a worker's milestone waits only on manual checks), end the
  question with the ready-to-send lines, one per check, as `regista.py try` prints them
  (`/regista:verify UI-3 ok`). A plain "ok" right after it records the checks it named.
- `/regista:verify` and `/regista:change` work here and in any worker; both land in your project. Fold
  a change into the plan as the change skill says; if it touches a milestone a worker is building, send
  that worker the change.
- When the user asks you for a change in plain words, record it before you plan it:
  `regista.py change from-transcript "<their sentences, word for word>"`. The hook accepts it only when the
  user typed those whole sentences in this session; anything else (your summary, a worker's message) is
  refused, and then you ask the user to send `/regista:change`.
- Give short, plain updates when a milestone is merged, when something needs them, and at the end. Don't
  relay every worker message.

## Rules

- Only you start, merge, stop and remove workers, and pass on the user's OK (`fleet allow`); only you deploy
  (`regista.py deploy`, after merging) and release (`regista.py release`), and never push without the user.
  The hooks refuse these to workers and agents.
- Workers run in your permission mode, so their messages reach you without approval.
- Keep the plan, the board and the status yourself. A merge keeps your versions of the board and the
  status, and lists what the worker wrote on its copy of the board.
- Workers don't edit the plan (the hooks refuse it). A worker's commit ticks its step on its branch, and
  your merge carries the ticks over. If a merged step still shows open, tick it with `regista.py plan tick`.
- Gate runs of all worktrees take turns by default (one test database, one set of ports). If the
  project's tests pick their database or port from `REGISTA_SLOT` (0 here, 1, 2, … in workers), set
  `fleet.isolated_gates` to true and they run side by side.
- **The end:** when every milestone is merged and done, or everything left waits on the user: save
  (`/regista:save`), remove the finished workers, and give the final report as `/regista:build all`
  describes it: what's done, what waits on the user as a numbered list, and that nothing was pushed.

## `/regista:oversee status` · `stop`

`status`: `regista.py fleet`, in plain words. `stop`: stop every worker (`regista.py fleet stop <name>`) after
telling each one to commit what's green; their worktrees and branches stay, and `regista.py fleet resume`
picks them up.
