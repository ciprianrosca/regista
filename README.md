# Regista: a build harness for Claude Code and Codex

**Coding agents are good at writing code and bad at keeping their own rules over a long project.** They
forget where they were after a reset, skip the tests when they're in a hurry, and approve their own work.
Regista makes the rules hold: every step gets its tests written first, an independent review of exactly
that change, and gates that block the commit until both pass. The work survives every reset, and you see
it in plain language.

Regista is built with Regista: every change since version 0.10 was specified, tested first, reviewed and
gated before it could be committed.

```bash
claude plugin marketplace add ciprianrosca/regista
claude plugin install regista@regista
```

Then, in your project: `/regista:init` to set it up, and `/regista:build all` to build the plan.

## What it enforces

Regista turns Claude Code, or OpenAI's Codex CLI, into a disciplined builder for long-running
projects. The rules that matter are **enforced by hooks**, not left to the model's memory:

| Rule | Enforced by |
|---|---|
| A session starts from the saved state: where we are, what's next, what's blocked | `SessionStart` hook loads it into context (again after every compaction) |
| No commit when tests fail or a metric gets worse | `PreToolUse` hook on `git commit` (Bash and PowerShell) runs the project's gates and denies the commit |
| Test suites run one at a time: a gate's command (`npm run e2e`) runs only through the harness's gate lock | `PreToolUse` hook refuses the command run directly and names `regista.py gate <name>` |
| No `--no-verify`, no editing the baseline to pass, no loosening the config without the user | `PreToolUse` hooks; the commit hook compares the config's checks with the last commit's |
| **Tests go only when proven redundant, and only with your OK**: the building flow never removes tests; `regista.py tests cleanup`, which only you run, lists overlapping tests and proves each adds nothing by breaking the code it guards | the commit hook asks you about any commit with fewer tests than before; agents are refused the command and its proof files |
| Protected files (ground truth, labels, secrets) are never written | `PreToolUse` hooks on Edit/Write and shell writes |
| No push without the user's OK | `PreToolUse` hook asks for permission on `git push`, and on `gh pr merge` or `gh release` |
| No turn ends with commits that no handoff covers | `Stop` hook sends Claude back to save |
| **Build everything**: `build all` goes through every milestone without stopping in between, and stops only for what needs you | `Stop` hook sends the session back to work while the run is active and keeps committing; a run that stalls ends itself |
| **An overseer and parallel workers**: you talk to one session; it starts a worker session per milestone that can be built alongside the others, each in its own git worktree, and merges their work behind the gates. You can also open any worker directly, and the overseer still hears about it | `regista.py fleet`; the workers' `UserPromptSubmit` and `Stop` hooks record what you tell them and what they did for the overseer, whose hooks show it; workers can't push, deploy, merge, start workers or edit the plan |
| Nothing is lost when a session dies or compacts without a save | `PreCompact` and `SessionEnd` hooks write a mechanical snapshot |
| **A context window compacts itself at 65%**: at 55% the agent is told to finish the step and checkpoint; at 65% the host compacts the conversation and the work carries on from the saved state, with nothing for you to clear or restart. If compaction doesn't happen, a hard pause at 75% saves the work instead | the hosts' own compaction, set by `regista.py setup`; `PreToolUse`, `PreCompact` and `SessionStart` hooks, measuring the session's real token use from its transcript |
| **Try it yourself**: what only a person can judge gets click-by-click steps, and only you can record the result, even from your phone while the session is busy | `UserPromptExpansion` (Claude Code) or `UserPromptSubmit` (Codex) records `verify` commands you type; messages queued while the session works are read from the transcript; agents are refused |
| **Change the plan mid-run**: `/regista:change <what you want>` is recorded and shown to the session until it's in the plan; "build it last" becomes an ordering rule | the same hooks record it; `SessionStart` and the build-all nudge show open changes |
| **What's live gets checked where it's served**: a deploy runs live checks, and the status says which commit is live | `regista.py deploy`; `PreToolUse` refuses the deploy command run directly |
| **Releases**: a version, a dated changelog entry, green gates (long checks on another server included), a tag, its release page and a deploy of the tag, in one command; a release above a patch is your call | `regista.py release`; `PreToolUse` asks you before an agent's release above a patch, and before its pushes |
| **Roles in separate contexts**: the main session orchestrates; a builder, a test writer, a reviewer and a researcher each work in their own context, with their own permissions | `PreToolUse` checks the acting agent on every tool call; `SubagentStart` tells each agent its role |
| **No commit without an independent review** of exactly that change | the commit hook checks an approving verdict whose fingerprint matches the current change set; only the reviewer can record one, and any later edit voids it |
| **Tests can't be gamed by the builder**: no edits to tests or test config by any route, and a legitimate `BLOCKED` exit when the spec can't be met | `PreToolUse` role rules (editor tools and shell writes); agent instructions |
| **Requirements pass only through the gates**, and a milestone is done only with end-to-end evidence | the commit hook refuses uncovered requirements and records pass/fail; no agent can write `.regista/gates/` or run `verify-manual` |
| **The guards fail closed**: a crashing hook or a broken config blocks the action; gates run within a time budget so the hook decides before the host's timeout would let the action through | error handling in `regista.py`; `HOOK_GATE_BUDGET`; in Claude Code also `onFailure: "block"`, so a hook that can't even start (no Python) blocks too |
| **Shell commands are judged by what they do**: what a command writes and reads, not the words in it, so probing in `/tmp` or in a scratch repository, inline scripts and heredocs don't trip the guards, and writing a guarded file by any route still does | `regista.py` splits each command like a shell (quotes, heredocs, redirects, `cd`, variables) and reads inline Python and JavaScript for the paths their write calls name; text in triple quotes is data, a path that ends in an unknown variable isn't the whole project, and a command that works on another repository is judged by that repository |

The logic is one standard-library Python script, `scripts/regista.py`, so it behaves the same every time.
Projects without a `.regista/config.json` are left alone: the plugin can stay enabled everywhere.

## Install

**Claude Code** (2.1.295 or newer):

```bash
claude plugin marketplace add ciprianrosca/regista
claude plugin install regista@regista
```

**Codex CLI** (0.160 or newer):

```bash
codex plugin marketplace add ciprianrosca/regista
codex plugin add regista@regista
```

Then, in Codex: open `/hooks` and trust the Regista hooks (Codex skips hooks you haven't trusted, and
asks again after each update), and trust each project folder when Codex asks (its `.codex/` settings
load only then).

**Updates:** `claude plugin marketplace update regista` (or the `/plugin` menu) in Claude Code;
`codex plugin marketplace upgrade` in Codex, then trust the updated hooks in `/hooks`.

**From a local clone** (to work on Regista itself), use the clone's folder instead of
`ciprianrosca/regista` in the commands above. Claude Code then loads the plugin in place: after
editing it, run `/reload-plugins` or start a new session. To try it for one session without
installing: `claude --plugin-dir <path to the clone>`. Codex copies the plugin into its cache, so run
`codex plugin add regista@regista` again after each edit.

Both need Python 3.10+ and Git (Git Bash runs the gate commands on Windows). In Claude Code the hooks
start through `scripts/regista`, a small launcher that finds Python as `python3`, `python` or `py -3`.
Codex starts them with `python`, so there the command must exist (`sudo apt install python-is-python3`
on Debian and Ubuntu); `regista.py doctor` checks it. Everything runs on Python 3.10 except one step:
editing ruff's settings (below) needs Python 3.11 or newer, because reading TOML came with 3.11. On 3.10,
`init` and `migrate` leave ruff's settings as they are and print the line to add by hand.

## Use

In a project's repo root (in Codex, `$regista:<name>` instead of `/regista:<name>`):

```
/regista:init          set up the project: detects the checks, asks three plain questions, writes a plan
/regista:init docs/spec.md   the same, taking the answers from a spec
/regista:start         where we are / next / blocked (runs automatically at session start)
/regista:build next    next step: brief → test writer → builder → reviewer → gates → commit
/regista:build M2      keep going until milestone 2's gate passes or a step is blocked
/regista:build all     build every milestone in the plan, one after another; stop only for what needs you
/regista:oversee       the same with parallel workers: this session oversees, workers build side by side
/regista:try           what to try yourself, step by step (you record it with /regista:verify)
/regista:verify UI-3 ok    record a check only you can make (or: fail <what happened>; all ok; M3 ok)
/regista:change <what you want>   change what gets built, at any time, also during a run
/regista:gate          run the gates (--all includes manual ones; --accept sets a new baseline)
/regista:decide <item> options and a recommendation for an open decision
/regista:save          gates, status, commit, handoff, board, learnings
/regista:pause         stop for the day: save everything and hand over
/regista:status · /regista:todo · /regista:learn
```

`regista.py deploy` deploys the last commit and runs the live checks (see Deploys below).
`regista.py plan add-milestone "<title>"` and `regista.py plan tick M3 <step>` change the plan (see Build
everything below).
`regista.py release` makes a release: version, changelog, gates, tag, release page, deploy (see Releases below).

`python <regista>/scripts/regista.py doctor` checks that everything works in a project, in plain
language, with the fix for each problem. It also checks that git knows who commits, and proposes a
`deploy` section when the project has live checks but no deploy, and names the gates whose linter would
also check the code in `.regista/` and in agents' worktrees (see "What a project gets").

To open a new session in a project from a terminal, in one line:

```bash
python "<regista>/scripts/regista.py" open ~/fpl fpl-build
```

It starts Claude Code in that folder inside tmux, with Remote Control under that name (so it shows up in
the Claude app), and answers the "do you trust this folder" question for you. `--mode auto` sets its
permission mode. Only you run it: agents are refused, as they are for starting any session.

## What a project gets

```
.regista/
  config.json     gates, protected paths, push/stop policies
  plan.md         milestones, each with a measurable gate
  status.md       latest results + Next (or point status_file at an existing file)
  TODO.md         decisions and human tasks
  state/          session handoffs (YYYY-MM-DD-slug.md) and automatic snapshots (-auto-)
  memory/         decisions and corrections
  knowledge.json  technical lessons
  gates/          baseline.json, requirements.json and deployed.json (committed; harness-written);
                  last.json, history.jsonl, passed.json and detached.json (local)
  changes.json    the user's changes to what gets built, and where each went in the plan
  reviews/        reviewer verdicts by change fingerprint (committed)
  research/       researcher notes
  specs/          one spec per step: requirement IDs and how each is verified
.claude/settings.json   Claude Code compacts at the limit (CLAUDE_AUTOCOMPACT_PCT_OVERRIDE)
.codex/                 if Codex is installed: config.toml (compaction; git access from the Windows
                        sandbox) and agents/regista-*.toml (the four roles)
AGENTS.md               a Regista section telling Codex what to do if the hooks aren't running
```

`/regista:init` runs `regista.py init --auto`, which reads the project's own files (package.json,
pyproject.toml, go.mod, Cargo.toml, Playwright or Cypress config, lockfiles) and proposes the gates, the
source and test folders and the protected secret files, so the user is asked only what only they know:
what they're building, how they'd check it works, and what must never change. A project without tests
or an end-to-end check starts with a milestone that adds them. When the end-to-end tests use a database
server (PostgreSQL, MySQL, MongoDB, Redis, or an ORM over one), the proposal adds a database per worker:
the tests' setup names it after `REGISTA_SLOT` (for example `shop_test_${REGISTA_SLOT:-0}`), so workers
don't reset each other's data and their gate runs needn't take turns (see "Tests that share a database").

Linters should leave `.regista/` and `.claude/worktrees/` alone: the first can hold research scripts, the
second holds agents' copies of the project. When the project has ruff settings (`[tool.ruff]` in
pyproject.toml, or ruff.toml or .ruff.toml), `init` and `migrate` add both folders to ruff's
`extend-exclude` and say so; the rest of the file stays as it was, and a second run changes nothing.
Regista doesn't edit other linters' settings: `regista.py doctor` names each gate that runs eslint,
flake8, pylint, biome, `prettier --check`, or a ruff that doesn't exclude both folders, while `.regista/`
holds code files, so you can add the excludes yourself.

### Roles

In `orchestrated` mode (the default) the main session never edits source or tests; it briefs
agents and keeps only their short reports. Each agent starts with an empty context.

| Agent | Does | The hooks refuse |
|---|---|---|
| main session (orchestrator) | picks the step, writes the brief, spawns agents, commits | edits to `roles.source` and `roles.tests` |
| `regista:test-writer` | writes the step's tests from the spec and the interface | reading or searching source (Read, Grep, shell), writing anything but tests |
| `regista:builder` | implements until the tests and gates pass | editing tests |
| `regista:reviewer` | reviews the change set, records a verdict | editing anything but its verdict file |
| `regista:researcher` | answers one question with sources | editing anything but `.regista/research/` |
| any subagent | | starting other agents, committing, pushing, writing reviews (except the reviewer) |
| other agents (Explore, general-purpose) | | editing source or tests in orchestrated mode |

Each agent gets its own context budget: past the limit, it's told to stop and report back.
`solo` mode lets the main session build (for small projects); the review gate still applies.

```json
"mode": "orchestrated",
"roles": {"source": ["src/**"], "tests": ["tests/**", "**/test_*.*"]},
"review": {"required_for": ["src/**"], "min_changed_lines": 15, "max_rounds": 3}
```

A change of at least `min_changed_lines` under `required_for` needs an approving review before it can
be committed. The reviewer writes `.regista/reviews/draft.json` and runs `regista.py review-record`,
which validates it (every finding needs file, line and severity; an approval needs a `checked` list
and no blocker or major findings) and stamps it with a fingerprint of the exact files. Commit the
verdict with the change: it's the audit trail. `regista.py review-status` shows where a change stands.

### Requirements and milestones

Each build step gets a spec, `.regista/specs/<step>.json`, with requirement IDs in testable form and
how each is verified: `test`, `e2e` (the real program, run by a gate with `"evidence": "e2e"`) or
`manual` (only you can check it, with `regista.py verify-manual ID --note "…"`; agents are refused).

- Tests mark what they cover: `covers:` and the IDs on the same line, separated by commas or spaces, in
  any comment style, a docstring or a test's name: `# covers: STATS-1`, `// covers: STATS-1, STATS-2`,
  `test("empty list (covers: STATS-1)", …)`. Only the marker counts: a test file that merely mentions an
  ID (in a comment, or an expected message such as "must look like NAME-1") doesn't cover it. A commit is
  refused while an active requirement has no marked test; when a test file mentions the ID without the
  marker, the refusal and `regista.py requirements` name that file, so an older project moves over by
  adding the marker where a test really checks the requirement.
- The commit hook records which requirements pass in `.regista/gates/requirements.json` when a commit
  goes through, and so does a gate run on a clean tree, which commits it: the results describe committed
  code, never half-finished edits. An entry changes only when its result, pass or fail, changes; the same
  pass proven by another gate (the live checks one time, the end-to-end tests the next) leaves it as it
  is. Agents can't write that folder, and the status file can't overrule it.
- `regista.py milestone M1` says whether M1 is done: every requirement passes and, by default, at least one is
  proven end to end. `regista.py requirements` lists coverage and results.

Why: in [ImpossibleBench](https://arxiv.org/abs/2510.20270) frontier models cheated on 50–76% of
impossible tasks, Claude mostly by editing tests despite being told not to; read-only tests and an
explicit way out cut that sharply. Verification failures and premature "done" are among the most common
multi-agent failures ([MAST](https://arxiv.org/html/2503.13657v3);
[Anthropic](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)).

### Build everything

Hand Regista a full spec (link it at the top of `plan.md`, or give it to `/regista:init`) and run
`/regista:build all`. It works through the milestones in plan order and doesn't stop between them:

- a milestone whose gate passes is saved, and the next one starts;
- a milestone that waits only on your manual checks is left for you, and the build moves on (under
  `"milestones": "strict"` the run stops there instead);
- a change you send with `/regista:change` (also from your phone while the session is busy) is folded
  into the plan before the next step; "build this last" or "after What-if" becomes an `After: all` or
  `After: M8` line on the milestone, and `regista.py run` lists the milestones in that order and names the next.
  A change you ask for in plain words is recorded too: the session runs `regista.py change from-transcript
  "<your words>"`, which the hook accepts only when you typed those whole sentences in this session;
- a step that needs your decision goes on the board with options and a recommendation, and the build
  carries on with the steps that don't depend on it;
- a step that stays blocked (an impossible spec, review rounds used up) goes on the board the same way.

`regista.py run start` marks the run active in `.regista/run.json`. While it is, the `Stop` hook sends the
session back to work each time it tries to end its turn, as long as it has committed something since
the hook's last nudge. A run that tries to stop twice without a new commit ends itself, so it can't trap
the session. A stop while agents the session started are still working doesn't count: the
`SubagentStart` and `SubagentStop` hooks record when each agent starts and finishes, the stop passes
quietly, and the agents' completion notices wake the session. An agent that never reports finishing
stops counting two hours after it started. The run survives compaction, and after a pause `/regista:start continue` picks it up
again. It ends with `regista.py run end --reason "…"` once every milestone is done or everything left waits
on you (the run then ends "waiting on you", not "stopped"), and the final report lists what you need
to do: decisions, checks to try, blocked steps.
Nothing is pushed. `regista.py run` shows where it stands.

**The plan** is changed through the harness. `regista.py plan add-milestone "<title>" --after M3 --files
"src/pages/**, tests/test_pages.py" --gate "<criterion>" --step "<step>"` writes a milestone in the plan's
format (repeat `--gate` and `--step`), and `regista.py plan tick M3 <number or text>` ticks a step. The
commit hook ticks a step itself when the step's commit goes through: its spec names the step
(`"plan_step"`: the step's text as the plan writes it, or its number in the milestone; failing that, a
`title` or `step` equal to the step's text) and the commit holds work besides the harness's records. If
the hook can't tell which step it is, it ticks nothing.

### The overseer and its workers

`/regista:build all` builds in one session, one milestone after another, and that session is busy for
hours: a message you send waits until it finishes its step. `/regista:oversee` splits the job. The
session you talk to becomes the overseer: it doesn't build, so it always has time for you. It starts a
worker for each milestone that can be built alongside the others, and keeps the plan, the board and the
status itself. Start the overseer in the project folder (`cd <project> && claude`): a session started
anywhere else gets none of the project's hooks, so it would hear nothing from its workers, and
`regista.py fleet spawn`, `merge`, `resume` and `remove` refuse to run from it. (From a plain terminal in
the project they work as usual.)

- **A worker** is a whole Claude Code session running the build loop with its own agents, in its own git
  worktree (`.regista/worktrees/<name>`, branch `regista/<name>`). It starts in tmux with Remote
  Control on, so you can open it from the Claude app as well (without tmux it runs as a background
  session, which you open with `claude attach`). Up to three run at once (`fleet.max_workers`).
- **What runs side by side**: milestones that no `After:` line holds back, which the overseer narrows to
  the ones that don't need each other's code or touch the same files. Fonts, navigation or anything else
  that touches every page runs alone, after the rest. A milestone names its files with a line under its
  heading, `Files: src/pages/**, tests/test_pages.py`, and `fleet spawn` warns (it doesn't refuse), naming
  the files, when the new worker's milestone shares any with a running worker's.
- **Staying in step**: the sessions message each other through Claude Code's own messages between
  sessions. The overseer hears when a worker pauses, and a worker tells the overseer when it finishes,
  gets blocked or needs an answer (its `Stop` hook reminds it). On top of that, the hooks record what
  happens in each worker for the overseer, whatever the models say: what you told it directly, its
  commits, its last words each turn. `/regista:verify` and `/regista:change` typed in a worker are
  recorded in the overseer's project.
- **Merging**: `regista.py fleet merge <name>` merges a worker's branch in one merge commit, made only when
  the commit gates pass on the merged code. The overseer keeps its own board and status (the merge lists
  what the worker wrote on its copy), and the harness's JSON records merge by content. Small things don't
  stop it:
  - a worker still finishing its turn gets up to a minute to pause (`fleet.merge_wait`; `--when-idle 600`
    waits up to ten minutes);
  - when the only uncommitted changes in the project folder are the harness's records (status, board, plan,
    requirement results), the merge commits them first, on their own; an untracked record that the branch
    brings is moved aside to `<name>.before-merge`, and the merge says so;
  - when both sides ticked steps in the plan, the ticks from both are kept (any other change to the same
    plan lines still stops the merge);
  - when the merge changes a dependency lock file (`package-lock.json`, `uv.lock`, `requirements.txt`, …),
    the dependencies are installed in the project folder (`fleet.setup`) before the gates run;
  - passing gate runs are remembered in the git folder all worktrees share, so merged code that is exactly
    what the worker just passed isn't tested again.

  A code conflict or a failing gate leaves the project untouched and says what to ask the worker.
- **Bringing the project's work into a worker**: `regista.py fleet sync`, run by the worker, merges the
  project's branch into its own: the harness's records merge by content, the overseer's status and board
  win, and the merge is committed when the gates pass. On a real code conflict it resolves the records,
  leaves the code to the worker, and the worker commits as usual.
- **Waiting for your OK**: when a worker wants to do something that needs you (a config change that
  loosens a check, say), it doesn't sit behind a permission prompt nobody sees. The overseer's news shows
  "waiting for your OK" at the top, and it asks you. Your yes can come through the overseer: it runs
  `regista.py fleet allow <name>`, which lets the worker do exactly that once. The hook lets the overseer
  run it only right after you typed yes in its chat, to a question that named that worker, so no agent can
  approve for you. You can also say yes in the worker itself.
- **A worker's own records are kept**: removing a worker deletes its worktree, and with it the records git
  doesn't hold: its hooks log (what its hooks refused), its gate history, its compactions, and any review it
  wrote but never committed. So `fleet remove` first copies them into the project, at
  `.regista/fleet/logs/<milestone>/<worker>/` (same file names, plus an `index.json` with the worker's name,
  milestone, branch, Claude Code session id, worktree and the time of the copy), whether the worker was merged
  or not and with or without `--force`. Reviews the project already has aren't copied again. If the copy
  fails, the worktree stays and the command says why. These records alone don't stop a plain `fleet remove`;
  other uncommitted changes still do. Like the hooks log and gate history, the folder stays out of git.
- **News, kept short**: the news drops workers that have been removed, and folds a worker's progress-only
  updates into one line. What's ready, blocked, waiting for you, or asking something comes in full.
- **Limits**: only the overseer starts, merges, stops and removes workers, passes on your OK, deploys, and
  (with your OK) pushes. A worker writes only in its own worktree, and the overseer doesn't edit a worker's files.
  A worker doesn't edit the plan either: its commits tick its steps on its branch, and the merge carries
  the ticks into the project.
  Workers run in the overseer's permission mode, because Claude Code holds messages from a session in a
  different mode for your approval.
- **Tests that share a database**: gate runs of all worktrees take turns by default, and agents can't run
  a gate's command outside them. If your tests choose their database or port from `REGISTA_SLOT` (0 in
  the project folder, 1, 2, … in the workers; the harness sets it for gate runs), for example a test
  database called `shop_test_${REGISTA_SLOT:-0}` that the test setup creates when it's missing, set
  `fleet.isolated_gates` to true and they run side by side. `/regista:init` proposes this when the
  end-to-end tests use a database server.

```json
"fleet": {"max_workers": 3, "setup": "npm ci --silent", "copy": [".env"], "isolated_gates": false,
          "claude_args": ["--model", "sonnet"], "merge_wait": 60}
```

`setup` runs in each new worktree (installing dependencies), and in the project folder after a merge that
changed a lock file; `copy` brings over local files the tests need; `claude_args` are extra options for
the workers' `claude` command; `merge_wait` is how many seconds a merge waits for a busy worker. `regista.py fleet` shows the
workers; `fleet show <name>` prints a worker's screen; `fleet stop`, `fleet resume` and `fleet remove`
do what they say. In a project Claude Code hasn't opened before, the first worker stops at Claude
Code's "do you trust this folder" question; `regista.py fleet spawn` says so, and you answer it once with
`tmux attach -t <session>`.

### Context budget

```json
"context": {"warn_pct": 55, "limit_pct": 65, "window": "auto", "on_limit": "compact"}
```

The harness reads the session's token use from its transcript on every tool call: Claude Code's
transcript (the same number the status line shows as used), or Codex's rollout (`last_token_usage`
against `model_context_window`). In Claude Code the window follows the documented defaults: 1M for
Opus 4.7+, Sonnet 5+, Haiku 5.5+ and Fable models, 200k for older ones unless they run as `[1m]`.
Set `window` (tokens), or the env var `REGISTA_CONTEXT_WINDOW`, to override it.

- **At `warn_pct`** the agent is told to finish the current step, commit it if the gates pass and write
  a handoff, so the saved state is complete.
- **At `limit_pct`** the host compacts the conversation itself and the agent carries on. `regista.py setup`
  sets this up: `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` in `.claude/settings.json` (it applies to subagents
  too), and `model_auto_compact_token_limit` (65% of your Codex model's window) plus
  `model_post_turn_compact_threshold_percent` in `.codex/config.toml`. Before compacting, a hook
  writes a mechanical snapshot. After it, the session-start hook loads the saved state again. Each
  compaction is logged to `.regista/compactions.jsonl`.
- **Backstop:** if the context still passes `pause_pct` (default `limit_pct + 10`), compaction didn't
  happen (auto-compaction switched off, or the setting missing), and the old pause applies: only saving
  is allowed (`.regista/`, the status file, the board, git, the Regista CLI), then `/clear` and
  `/regista:start continue`. `"on_limit": "pause"` makes that the rule at `limit_pct` itself.

`python scripts/regista.py context <transcript.jsonl>` shows a Claude Code session's current use.

### Refusals, and whether they were right

Every time a hook refuses a tool call, or asks you before letting it through, Regista adds one line to
`.regista/hooks.log`. The line says whether it was a refusal or a question, which rule fired, whose call it
was (the main session, which agent, or which worker), the session, and a first label. Rules that are never
wrong when they fire, such as a failed gate, a commit with `--no-verify`, a write to a protected path or a
push that needs your go-ahead, are labelled `real`. The others, mostly the rules about what each agent may
touch, are labelled `unsure`, because some of them are false alarms (a reviewer's read-only command judged
to change a file, for example). A rule about paths counts as `real` only when the refused path matched
exactly: when the match came from a folder that holds it (`git checkout -- .`), a variable Regista couldn't
follow, or a path inside inline code, the refusal is `unsure`. So is a pause at the context limit, which
rests on an estimate of the tokens used.

```bash
python scripts/regista.py refusals                 # every refusal, newest first, with its number
python scripts/regista.py refusals --unlabelled    # only the unsure ones, waiting for your label
python scripts/regista.py refusals label 12 false_alarm --why "sed -n only reads the file"
python scripts/regista.py refusals --json          # the same list for other tools, labels included
```

The label can be `real` (the refusal was right) or `false_alarm` (it shouldn't have fired). Your labels are
added to the end of the log, so nothing already in it is ever rewritten, and older lines without a rule or
a label still list. Labelling is your call: the hooks refuse every session that tries to run `refusals label`,
the main orchestrator session included, so you label from your own terminal, and they refuse every write to
the log they can recognise. As a second defence, `refusals label` itself won't run inside a Claude Code or
Codex tool shell (it sees their environment variables, such as `CLAUDECODE`). Neither defence is complete:
a command the hooks can't parse (inline Python, a renamed copy of `regista.py`, a variable cleared first)
could still add a label or write to the log. The board tracks that gap; closing it needs a guard at the
operating-system level, such as a separate user or a read-only mount.

### Try it yourself

Some requirements only a person can judge: does it look right, is it fast enough on their phone, does
the flow make sense. They're `"verify": "manual"` in the spec, with steps someone who doesn't code can
follow:

```json
{"id": "UI-3", "text": "…", "verify": "manual",
 "try": {"title": "Sign-up works on a phone", "steps": ["Open http://localhost:3000/signup", "Sign up"],
         "expect": "You land on your dashboard and see your name"}}
```

`/regista:try` shows the checklist and starts the app if the steps need it. You record what you saw:

- in Claude Code, `/regista:verify UI-3 ok` or `/regista:verify UI-3 fail <what happened>`. The
  `UserPromptExpansion` hook records it: it fires only for a command a person typed, and the model
  can't invoke that command. Sent while the session is busy (from your phone over Remote Control, say),
  Claude Code folds the message into the running turn without any hook; the hooks then find it in the
  session's transcript, where Claude Code marks it as typed by a person, and record it once. Nobody may
  write the transcripts. Several checks fit in one message, one per line, and `all ok` (or `M3 ok`)
  records every check still waiting.
- in Codex, `$regista:verify UI-3 ok`. The `UserPromptSubmit` hook records it: in Codex it fires only
  for a person's input (subagents talk through tool calls, and a subagent's turn carries its id).
- in any terminal, `python regista.py try --ask`, which walks you through each check.
- by replying "ok" to the question that asked for the checks. Every question that asks you to try
  something ends with the ready-to-send `/regista:verify <ID> ok` lines. Right after it, a plain "ok" or
  "all ok" records every check it named, "UI-3 ok" just that one, "1. ok" item 1 of a numbered
  question, and "doc ok" the only check it named. Only a reply you typed counts, and only when the
  session's last message held those lines; "not ok" or "mostly ok" records nothing.

Each manual record also says how it was passed, in its `how` field, without changing whether it passes:

- `looked`: you passed that one check by name (`/regista:verify UI-3 ok`, an "ok" to a question naming only
  that check, `try --ask`, or `regista.py verify-manual` run yourself at a real terminal).
- `bulk`: passed together with others, by `all ok`, `M3 ok` or a plain "ok" to a question naming several
  checks. A check you had already passed by name keeps `looked`.
- `forced`: `verify-manual ID --note "…" --forced`, or a note that says it was forced, not confirmed
  individually or not checked one by one.
- `relayed`: `verify-manual` run without a terminal, or in a shell that carries an agent's environment
  variables (`CLAUDECODE` or Codex's, the same check `refusals label` makes), even inside a pseudo-terminal.
  That covers an agent in another project's session, where this project's hooks can't refuse it. `by` still
  says `user`.

`try --ask` won't run at all in a shell that carries an agent's variables, even inside a pseudo-terminal: it
records nothing and tells the agent to ask you to run it in your own terminal.

This is not a guarantee that an agent can never get `looked`. Both commands recognise an agent's shell only by
its environment variables, so a command that clears them and runs `verify-manual` or `try --ask` inside a fake
terminal could still pass as `looked`. It is the same gap as the one `refusals label` has, described above, and
closing it needs the same guard at the operating-system level.

`regista.py requirements` shows the how next to each manual pass (`unknown` for records written before the
field existed), and `regista.py milestone M2` says how many of the milestone's passed manual checks you
didn't look at one by one.


Agents are refused `verify-manual`, `try --ask`, `regista.py open` and starting Claude Code or Codex sessions from the
shell (a nested session could type the command for them). `claude plugin`, `claude mcp`, `claude agents`,
`claude doctor` and `claude --version` start no session and are fine, and so is the harness run on a
scratch project of its own. A failed check goes on the board and back
through the build loop.

The hooks are never run by hand. Only Claude Code and Codex call them, with what you actually typed; fed a
made-up `/regista:verify UI-3 ok`, the user-prompt hooks would record a check you never looked at. So the
pre-tool hook refuses every agent's shell command that runs `regista.py hook …` or the `regista` wrapper's
`hook …`, however it is spelled (`python` or `python3`, `sh` with the wrapper, a quoted path, after `cd` or
`env`, with input piped in), in every role and in Codex too. A subcommand it can't read, such as `$(…)` or a
variable, counts as `hook`. The harness's hooks run on a scratch project of its own (another folder, with
`CLAUDE_PROJECT_DIR` cleared) are fine. That leaves gaps, as with the guard on `refusals label`: it can't see a
renamed copy of `regista.py`, inline code that imports it, or a run from another folder whose input names this
project as its `cwd` (a hook takes its project from its input).

### Codex

The same `regista.py` runs Codex's hooks with `--host codex` (`hooks/codex-hooks.json`, loaded through
`.codex-plugin/plugin.json`). What differs:

- **Edits** arrive as `apply_patch`: each file in the patch is checked like an edit, and a patch whose
  files can't be read is refused.
- **Roles** are Codex agents, `regista-builder`, `regista-test-writer`, `regista-reviewer` and
  `regista-researcher`. `regista.py setup` writes them to `.codex/agents/` from `agents/*.md`. Codex's hooks
  name the acting agent, so the same role rules apply.
- **No "ask"**: Codex hooks can only allow or refuse, and an unknown answer counts as a failed hook,
  which lets the action through. So a push or a harness config change is refused, the agent asks you in
  the chat, and your own "yes" (seen by `UserPromptSubmit`) allows that exact action once. Workers do
  the same in Claude Code, so their questions reach the overseer.
- **Decisions are JSON on stdout** with exit 0: Codex runs hooks through PowerShell on Windows, which
  turns exit code 2 into 1.
- **Windows sandbox:** Codex's elevated sandbox runs commands as another user, and git refuses a
  repository owned by someone else. `regista.py setup` adds this folder as a git `safe.directory` for the
  commands Codex runs here, through `.codex/config.toml`, so commits work.
- **Limits:** Codex lets an action through when a hook can't start (no Python) or times out, and runs
  hooks only once you've trusted them. `regista.py doctor` checks the setup, and `AGENTS.md` tells Codex to
  warn you when the Regista state is missing from its context, which means the hooks aren't running.

### Gates

```json
{"name": "tests", "run": "uv run pytest -q", "when": ["commit"], "timeout": 900}
{"name": "accuracy", "run": "uv run app harness --json", "when": ["commit"], "paths": ["src/geometry/**"],
 "metrics": {"p95_mm": {"max": 1.5}, "median_mm": {"max": 1.0, "tolerance": 0.02}}}
```

- `run` is a shell command run from the project root (Git Bash on Windows). Exit 0 = pass. It runs
  without `CLAUDE_PROJECT_DIR`, so a test suite that starts tools reading it works on its own folder, not
  on the session's project.
- `metrics`: the command prints one JSON line with numbers as its last output line. Each metric has
  `max` or `min`, and by default must not get worse than the accepted baseline (`no_worse`, with an
  optional `tolerance`). `regista.py gate <name> --accept` stores a new baseline.
- `when_changed` (older name: `paths`): run the gate only when the change touches matching files. The change
  is what the gate checks: at a commit, the uncommitted files; at a worker's merge, what the merge brings in;
  at a deploy, what changed since the last deploy whose checks passed; at a release, what changed since the
  last release's tag. A gate it leaves out is listed as "not needed for this change", its requirements keep
  their last result and its baseline stays. When the change isn't known (no earlier deploy or release, a
  commit git can't find) or is empty, the gate runs: a change never skips a gate it should run.
- **A commit runs the current milestone's end-to-end tests, not the whole browser suite.** An end-to-end gate
  (`"evidence": "e2e"`) with a `run_some` command runs, at an ordinary commit, only its test files whose
  `covers:` markers name requirements of the milestones the commit works on, plus the test files it changes:
  `{"name": "e2e", "run": "npx playwright test", "run_some": "npx playwright test {files}", "evidence": "e2e",
  "tests": ["e2e/**"]}`. Those milestones are a worker's own milestone, the milestones of the specs the
  commit changes, of the requirements its changed tests cover, and of the plan milestones whose `Files:`
  line it touches. The whole suite runs whenever that can't be told, when the commit may close a milestone
  (its milestone has one open step left, or none), at a worker's merge, before a deploy (a failure stops the
  deploy) and with `regista.py gate`, so nothing reaches the main branch at a milestone's end or the live
  site without it. A partial run records results only for the requirements its test files cover, and its
  pass never stands in for the whole suite. Without `run_some`, or for a gate with metrics, the whole
  gate always runs.
- **A time budget.** `"gates_budget_seconds": 300` in the config (for a whole run) or `"budget_seconds"` on a
  gate: when a run takes longer, the output and the next session's summary say so and suggest running
  `regista.py tests cleanup` to trim the tests. It only warns; nothing runs or changes on its own.
- `when`: `["commit"]` (the default), `["deploy"]` (after a deploy, see Deploys), `["release"]` (see Releases),
  `["milestone"]` (long checks a milestone needs, below), `["manual"]` (only on `/regista:gate --all`).
- A gate that times out fails. Keep the commit gates' total time well under the hook's 60 minutes.
- **One run at a time, and never twice for the same files.** Gate runs in a project take turns (two
  browser suites on one test database collide). A gate that passed on exactly the current files (the
  harness's own records aside) isn't run again: the builder's or the reviewer's passing run counts for
  the commit. `regista.py gate --fresh` runs everything anyway; `"reuse_passing_runs": false` turns it off.
- **Gate commands run only through the harness.** An agent (or the main session) that runs a gate's
  command itself, `npm run e2e` with or without extra arguments, is refused and told to run
  `regista.py gate e2e`, which takes the lock. A gate's command is the last one in its `run` that isn't
  setup (`cd`, `export`); `npm run e2e` and `npm run e2e -- --grep x` both count as the gate
  `npm run e2e --silent`, while a command that only mentions it (`echo`, `grep`) or runs one test file
  directly doesn't. The refusal is the same in Claude Code and Codex: Codex hooks can't rewrite a command.
- **A gate run on a clean folder commits its record.** When nothing but the harness's own records is
  uncommitted, `regista.py gate` (and a deploy's live checks) records which requirements pass and commits
  that record on its own, with a plain message ("Requirement results after a gate run: NAV-1 now
  passes"); `gate --accept` commits the new baseline the same way. Nothing else staged goes in, and a
  commit of records only skips the gates and the review. With other uncommitted work the record is left
  alone and the output says so: the commit hook records it with the commit.
- **Commits with nothing to check skip the gates**: the setup commit, and commits of only the harness's
  records (status, board, specs, reviews) or of `no_gates_for` paths (`["docs/**", ".regista/**"]` in new
  projects; a change to an already committed `.regista/config.json` still runs them). An untracked folder
  that is a git repository or worktree of its own, such as `.claude/worktrees/<name>/`, isn't part of a commit
  that names its files. A commit that would stage it (`git add -A`, `git add .`, or `git add` of a folder above
  it) is refused, because git would record it as a link to another repository instead of files: add the folder
  to `.gitignore` or stage your files by name. `regista.py init` adds `.claude/worktrees/` to `.gitignore`.
- **Loosening needs you.** A commit whose config removes or changes a gate, unprotects a path, or turns a
  check off asks for your OK, however the file was changed. Adding checks doesn't. An edit to the config
  is judged the same way as it's made: adding a gate or a protected path goes through, a change that
  loosens something asks, and a change the hook can't read in advance (from the shell, say) asks too.

**Long checks run elsewhere.** A check that takes an hour, on a staging server or a machine with the right
hardware, would hold every commit. A gate with `"kind": "detached"` starts it there and collects the result
later:

```json
{"name": "real", "kind": "detached", "when": ["milestone", "release"], "when_changed": ["src/ruler/**"],
 "start": "python tools/real_detached.py {sha} origin/main {label}",
 "status": "ssh staging 'cat ~/ci/out/{label}.result 2>/dev/null || echo running'", "timeout": 7200}
```

- `regista.py gate real --start` runs `start` with `{sha}` (the commit), `{short}` and `{label}` (a name for
  this run, like `real-1a2b3c4-1028-153000`) filled in, also given as `REGISTA_SHA` and `REGISTA_LABEL`. The
  command launches the run elsewhere (under its own name, so a closed ssh session can't kill it) and returns.
  The run is recorded against that commit; `--ref <commit>` checks another one, a pull request's head say.
- `regista.py gate --collect` runs each open run's `status` command. Its last line says how it went: `pass`,
  `fail`, `running`, or the gate's JSON line (with `metrics`, judged like any gate's; with or without a
  `"status"`). Anything else, or a server that doesn't answer, leaves the run open. Session start asks once
  too, within 20 seconds. A run that hasn't finished within `timeout` seconds (default 6 hours) fails.
- A collected pass counts for the code it ran on: the same commit, or a later one where only the harness's
  records changed, or, with `when_changed`, where none of those paths changed. Changing `start` voids it.
- Commits never wait for a long check: the commit hook leaves it out. A milestone with it in `when` isn't
  done (`regista.py milestone`) and a release with it isn't made until a pass on the current code is
  collected. `regista.py gate real` shows where it stands.

### Cleaning up the tests

Suites grow by repetition: the same check written twice, or one browser check run once per page. The building
flow never removes a test, because the agents trying to get a commit through shouldn't decide which checks to
drop: a commit with fewer tests than the last one needs your OK. When the gates get slow, you run the cleanup
yourself, in your own terminal (agents are refused it):

```bash
python regista.py tests cleanup                              # list overlapping tests; nothing is touched
python regista.py tests cleanup --prove 2026-10-28.json      # prove which ones add nothing
python regista.py tests cleanup --apply 2026-10-28.json      # remove the proven ones, after you type yes
```

1. **Propose.** It reads the test files (`roles.tests` and the end-to-end gates' `tests`) and finds tests that
   cover the same requirements on the same code, or whose bodies are nearly the same (`--similar 0.8`). It
   writes `.regista/cleanup/<date>.md`: each candidate, why it could go and which test replaces it, plus
   browser checks repeated per page that could move down to a unit test (those are only suggestions: the
   command never removes them).
2. **Prove.** On a scratch copy of the project, never the project itself, it breaks the code each candidate
   guards in simple ways (a comparison flipped, a constant changed, a return value dropped; `--mutations 8` per
   file), runs the commit gates with the candidate removed, and checks the tests still fail. A candidate is
   proven only if every break the full set catches is still caught without it. Candidates are proven one after
   another with the earlier proven ones already removed, so any set of proven ones can go together. It stops
   at `--budget 30` minutes and says what it couldn't prove, and why.
3. **Apply.** It shows what it will remove and waits for you to type `yes`. Then it removes only proven
   candidates (all of them, or the ones you name: `c1 c3`) and commits, with what was removed and the proof in
   the commit message. If a test or the code it guards changed after the proof, it refuses: prove again.

The command works with Python and JavaScript or TypeScript test files (`def test_…`, `test(…)`, `it(…)`), and
runs whatever commands your commit gates run.

### Deploys

The gates check the repository. What a deploy serves can still differ (cached assets, data built by the
old code), so a project that deploys says how, and how to check the result where it's served:

```json
"deploy": {"run": "bash deploy/deploy.sh", "timeout": 1200},
"gates": [{"name": "live", "run": "node --test tests/live/", "when": ["deploy"], "evidence": "e2e", "tests": ["tests/live/**"]}]
```

`regista.py deploy` deploys the last commit (it refuses uncommitted work), then runs the `deploy` gates and
records which commit is live and whether its checks passed (`.regista/gates/deployed.json`), committing
that record and the requirement results on their own. A deploy gate with
`when_changed` runs when those paths changed since the last deploy whose checks passed.

`"ref": "tag"` makes the server run tagged releases only: `regista.py deploy` deploys the release tag of the
current code (`regista.py release` makes one, and deploys it), and refuses when there's none. The deploy
command gets the tag as `{ref}` (and `REGISTA_DEPLOY_REF`), for example
`"run": "git push staging '{ref}^{commit}:refs/heads/main' && git push staging {ref}"`. Session
start, `regista.py run` and the final report say what's live and how many commits aren't. Agents deploy only
this way: running the deploy command directly is refused, so the live checks always follow a deploy. A
requirement proven by a live test (`covers: LIVE-1`) passes only through a deploy.

### Releases

```json
"release": {"version_files": ["pyproject.toml", "uv.lock"], "changelog": "CHANGELOG.md",
            "heading": "## {version} — {date}", "tag": "v{version}", "github_release": true, "deploy": true}
```

`regista.py release` (or `release minor`, `release major`; the default is a patch) does, in order:

1. Works out the version. The last number goes up by default (0.5.2 to 0.5.3). When the version files
   already carry a version that has no tag yet (a pull request set it), that version is released as it is.
2. Checks what a release needs, and says plainly what's missing, changing nothing: the version files agree,
   the changelog has an entry for the new version headed as `heading` says, dated today, with something in
   it, and nothing else is uncommitted (the new changelog entry may be).
3. Sets the version in every file in `version_files`. pyproject.toml, Cargo.toml, package.json and the
   project's own entry in uv.lock or Cargo.lock need nothing more; give any other file as
   `{"file": "app/version.py", "match": "__version__ = \"([^\"]+)\""}`, the version in brackets.
4. Runs the commit gates and the `release` gates, judged against what changed since the last release, on the
   new version. Long checks need a collected pass on this code; the release's own version and changelog
   edits don't void one. If a gate fails, the version files go back as they were.
5. Commits "Release 0.5.3" with the changelog, and tags it (`tag`, message `message`).
6. Pushes the tag and the branch together. The project's `push` policy applies: with `"ask"`, an agent's
   release waits for your OK; with `"block"`, the release stops here and lists what's left for you.
7. With `github_release`, publishes the release page with the changelog's section as its notes (it needs
   GitHub's `gh` command).
8. With `deploy` (the default, when the project deploys), runs `regista.py deploy`.

A release above a patch is your decision: when an agent runs `release minor` or `release major` (or the
files carry a bigger version), the hook asks you first; in Codex it's refused and the agent asks you in the
chat. `regista.py release --check` shows what a release would do and what's missing, and `--no-push` stops
after the tag. Only the main session releases; workers and agents are refused.

**Merging through pull requests.** Regista's workers merge with `fleet merge`, locally. A project whose
builders open pull requests on GitHub can use these pieces around them today:

- Start the long checks on the pull request's head before merging: `git fetch origin pull/77/head`, then
  `regista.py gate real --start --ref FETCH_HEAD`, and `--collect` it later.
- Merge on GitHub, then `git pull`. If the merge commit has the same code as the head the long check ran on,
  its pass still counts.
- Release with `regista.py release`: if the pull request set the version and wrote the changelog entry, the
  release tags it as it is, pushes the tag, publishes its page and deploys it.

A later version would need: `fleet merge` opening a pull request for each worker instead of merging
locally, waiting for GitHub's checks and merging with `gh` under the push policy (the hooks already treat
`gh pr merge` and `gh release` like pushes); GitHub's check results read as a gate; long checks started on
each pull request's head as it opens; a pull request's own branch merged with the project first when the
project moved on; and a watch on GitHub (new pull requests, finished checks, comments) that reports to the
overseer.

## Develop

`python -m unittest discover -s tests` drives every hook the way Claude Code and Codex do (JSON on
stdin) against throwaway git repos. `claude plugin validate .` checks the Claude Code manifests.

## License

MIT, see [LICENSE](LICENSE).
