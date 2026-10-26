---
name: build
description: "The Daedalus build loop: take the next step of the current milestone (or a whole milestone, or every milestone in the plan) and get it built, tested, reviewed, gated and committed, using the builder, test-writer, reviewer and researcher agents in their own contexts. Also 'decide <item>' for open decisions. Use when the user says 'daedalus build', 'build next', 'build M<n>', 'build all', 'build everything', 'keep building' or 'daedalus decide'."
---

# Build: orchestrate the step, don't do it yourself

The Daedalus CLI: `python "${CLAUDE_PLUGIN_ROOT}/scripts/dd.py"` (`python3` where there's no `python`; the
session-start context shows the exact command for this machine). Spec: `.daedalus/plan.md` plus any
spec it links (the linked spec wins where they disagree). Progress: the status file.

In **orchestrated** mode (`config.json → mode`, the default) you are the orchestrator. The hooks
refuse your edits to source and test files; the work happens in four agents, each in its own
context, so your context holds only the plan and short reports. In **solo** mode you build yourself
(skip steps 4 and 5 and do them directly), but the review gate still applies.

| Agent (`subagent_type`) | Does | Can't |
|---|---|---|
| `daedalus:researcher` | answers one question with sources | edit anything but `.daedalus/research/` |
| `daedalus:test-writer` | writes the step's tests from the spec | read the implementation, write outside tests |
| `daedalus:builder` | implements the step until its tests and gates pass | edit tests, commit |
| `daedalus:reviewer` | reviews the change set, records a verdict | edit anything but its verdict |

Agents start empty: they know only what you put in the prompt and what they read from files. The
start hook tells each one the project root, the CLI path and its own rules.

## `/daedalus:build next` · `/daedalus:build M<n>`

1. **Resume** if this session hasn't (`/daedalus:start`).
2. **Milestone gates: build in parallel, gate the claims** (`config.json → milestones`).
   - `parallel` (default): the next milestone's code may be built while an earlier milestone's gate
     is still open. But a milestone is marked **done**, and its results called final, only after
     its gate passes on the evidence the plan requires (e.g. real data, not synthetic). The status file
     keeps **build status** and **gate status** separate for every milestone. Say which gates are still
     open when you start building past them.
   - `strict`: the previous milestone's gate must pass before any of the next milestone's steps start.
     If it hasn't, say which criterion is open and stop.
3. **Pick the step and write its spec.** The first unchecked step of the current milestone. If it
   depends on an open decision on the board, stop and run `decide`. Never guess a decision that
   belongs to the user. Write `.daedalus/specs/<step>.json`:
   ```json
   {"step": "stats", "milestone": "M1", "title": "Mean and median",
    "interface": "calc.stats.mean(xs: list[float]) -> float; median(xs) -> float",
    "requirements": [
      {"id": "STATS-1", "text": "WHEN xs is empty, mean SHALL raise ValueError", "verify": "test"},
      {"id": "STATS-2", "text": "WHEN `calc mean 1 2 3` runs, it SHALL print 2.0", "verify": "e2e"}]}
   ```
   One requirement per observable behaviour, in testable form (WHEN … SHALL …), IDs unique across the
   project. `verify`: `test` (unit or integration), `e2e` (the real program, run by the e2e gate) or
   `manual` (only the user can check it). A milestone needs at least one `e2e` requirement. Keep it to
   one page. Every `manual` requirement gets a `try` block the user can follow without reading code:
   `"try": {"title": "…", "steps": ["Open http://localhost:3000/signup", "…"], "expect": "what they should see"}`
   (see `/daedalus:try`). When requirements change later, **change the spec first**, then the tests, then the code.
   Anything over about a day of work: show the spec to the user before going on.
4. **Research (only if needed).** One `daedalus:researcher` per open question. Pass the question
   and why it matters. Keep its answer, not its reading.
5. **Tests first.** Spawn `daedalus:test-writer` with the spec's path and the interface. It can't see
   the code, so the interface must be explicit. Its tests name the IDs they cover (`covers: STATS-1`).
6. **Build.** Spawn `daedalus:builder` with the spec's path, the test files to satisfy, and the
   relevant rules and lessons (or tell it which memory files to read). It returns `DONE` or
   `BLOCKED: <IDs> — <why>`.
   - **BLOCKED is a valid answer, not a failure:** the spec is impossible, contradictory or ambiguous.
     Don't push the builder to "make it pass". Fix the spec (a decision for the user if it changes what
     gets built), then send the affected tests back to a new test writer, then build again.
7. **Review.** Spawn `daedalus:reviewer` with the spec's path. Don't pass the
   builder's report or reasoning: the reviewer judges the code, not the story. It records its verdict
   with `review-record`.
   - `approve`: go on.
   - `changes`: spawn a **new** builder with the findings (file:line and the issue), then a **new**
     reviewer. After `review.max_rounds` rounds (default 3) without approval, stop and ask the user.
8. **Commit.** `git add -A && git commit -m "<type>: <step>"`. The commit hook checks that every
   active requirement is named by a test, runs the gates, records which requirements pass, and checks
   that an approving review matches exactly this change set. If it blocks, read why and fix it through
   the agents; don't work around it. Commit the spec and `.daedalus/reviews/<fingerprint>.json` with
   the change: they are the audit trail.
9. **Checkpoint.** Tick the step in the status file and update "Latest results". **Whether a
   requirement passes, and whether a milestone is done, is the harness's call, not yours:** copy it from
   `python "${CLAUDE_PLUGIN_ROOT}/scripts/dd.py" requirements` and `… milestone M<n>` (exit 0 = done).
   Never write "done" for a milestone the harness says is open. Record lessons with `/daedalus:learn`.
   If the milestone now waits only on manual checks, run `/daedalus:try` and let the user check it.
10. **Continue** with the next step. Stop and run `/daedalus:save` when the milestone is done or the
    next step is blocked. When the context warning appears, finish the step, commit it, run
    `/daedalus:save`, and keep going: at the limit the session compacts itself and you continue from the
    saved state (with `context.on_limit: "pause"`, run `/daedalus:pause` instead).

For `build M<n>`, repeat steps 3–9 until the milestone's gate is met or a step is blocked, then save
and report: steps done, gate numbers, review rounds, and what's blocking, if anything.

## `/daedalus:build all`

Build the whole plan, milestone after milestone, without stopping in between. It's for the user who
hands over a full spec and wants to come back to the finished product, or to one list of the things
only they can do.

1. **Resume** (`/daedalus:start`) and read the plan and any spec it links. If the plan doesn't cover
   the linked spec yet (milestones missing, steps missing), add them first, each milestone with its
   gate and at least one end-to-end requirement. Show the plan to the user only where it changes or
   leaves out something the spec asks for.
2. **Start the run:** `python "${CLAUDE_PLUGIN_ROOT}/scripts/dd.py" run start`. While the run is active, the
   Stop hook sends you back to work each time you try to end your turn, as long as you've committed
   something since its last nudge. A run that stops twice without a new commit ends itself, so it can
   never trap the session. `dd.py run` shows every milestone, done or open, as the harness sees it.
3. **Loop:** take the milestone `dd.py run` names as next (plan order, adjusted by `After:` lines) and
   run steps 3–9 above for its steps. The user gave the go-ahead for everything the plan and its spec cover, so, unlike
   `build M<n>`:
   - **Milestone done** (`dd.py milestone M<n>` exits 0): run `/daedalus:save`, then go straight on to
     the next milestone.
   - **Only manual checks left:** note them for the final report and go on to the next milestone
     (`milestones: "parallel"`, the default). Under `"strict"`, end the run there.
   - **A step needs a decision that's the user's:** put it on the board the way `decide` does
     (context, options, your recommendation), then skip that step and every step that depends on it,
     and carry on with the ones that don't. Never pick the answer yourself.
   - **A step stays blocked** (the builder says `BLOCKED` and the fix would change what gets built, or
     the review rounds run out): put it on the board with what you tried, skip it and its dependents,
     carry on.
   - **A big step** (over about a day): don't stop to show its spec if it only restates what the user's
     spec asks. If writing it forced a choice that spec doesn't make, that's a decision: see above.
   - **Context warning:** finish the step, commit it, run `/daedalus:save`, and carry on. The session
     compacts itself at the limit and the run survives it.
   - **A change from the user** (`/daedalus:change`, shown at session start and in every nudge): fold it
     into the plan before the next step, as the change skill says, then `dd.py change done <n> --where M<n>`.
     "Build this last" or "after What-if" becomes an `After: all` or `After: M8` line on the milestone.
   - **Deploying** (when `config.json` has a `deploy` section): deploy only with `dd.py deploy`, which runs
     the live checks after it; never run the deploy command yourself. Deploy after a milestone that
     changes what's live, and before the final report.
   - **Never push.** Pushing stays the user's call (the hook asks anyway).
4. **End the run** when every milestone's gate is met, when everything left waits on the user, or
   when the user asks you to stop (no change from the user may be left open): `/daedalus:save`, then
   `python "${CLAUDE_PLUGIN_ROOT}/scripts/dd.py" run end --reason "<all milestones done | what waits on the user>"`.
5. **Final report**, in plain words: the milestones done (copied from the harness, not from notes),
   what's built but still has an open gate, and a numbered list of what the user needs to do: each
   decision with your recommendation, the checks to try (`/daedalus:try`), the steps that stayed
   blocked and why. Say that nothing was pushed.

## Prompts to agents
- Self-contained: the agent has none of this conversation. Name files by path.
- Short and specific: the brief, the files, what "done" means, the report format you want.
- Never ask an agent to approve its own work, and never forward one agent's reasoning to the
  reviewer.

## `/daedalus:decide <item>`
For an open decision on the board: one short paragraph of context, 2–4 options with their cost and
effect on the build, a recommendation, and what it blocks. The user decides. Record the answer with
`/daedalus:learn` and tick it on the board.

## Rules
- Absolute dates. A number counts only when a gate or a harness produced it, and says how.
- One step at a time, each committed green and reviewed. Anything half-built across sessions goes
  in the handoff's "In flight".
- Secrets only in gitignored files. Never commit them, print them or put them in a handoff.
