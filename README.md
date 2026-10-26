# Daedalus: a build harness for Claude Code and Codex

Daedalus turns Claude Code, or OpenAI's Codex CLI, into a disciplined builder for long-running
projects. The rules that matter are **enforced by hooks**, not left to the model's memory:

| Rule | Enforced by |
|---|---|
| A session starts from the saved state: where we are, what's next, what's blocked | `SessionStart` hook loads it into context (again after every compaction) |
| No commit when tests fail or a metric gets worse | `PreToolUse` hook on `git commit` (Bash and PowerShell) runs the project's gates and denies the commit |
| No `--no-verify`, no editing the baseline to pass, no loosening the config without the user | `PreToolUse` hooks; the commit hook compares the config's checks with the last commit's |
| Protected files (ground truth, labels, secrets) are never written | `PreToolUse` hooks on Edit/Write and shell writes |
| No push without the user's OK | `PreToolUse` hook asks for permission on `git push` |
| No turn ends with commits that no handoff covers | `Stop` hook sends Claude back to save |
| **Build everything**: `build all` goes through every milestone without stopping in between, and stops only for what needs you | `Stop` hook sends the session back to work while the run is active and keeps committing; a run that stalls ends itself |
| Nothing is lost when a session dies or compacts without a save | `PreCompact` and `SessionEnd` hooks write a mechanical snapshot |
| **A context window compacts itself at 65%**: at 55% the agent is told to finish the step and checkpoint; at 65% the host compacts the conversation and the work carries on from the saved state, with nothing for you to clear or restart. If compaction doesn't happen, a hard pause at 75% saves the work instead | the hosts' own compaction, set by `dd.py setup`; `PreToolUse`, `PreCompact` and `SessionStart` hooks, measuring the session's real token use from its transcript |
| **Try it yourself**: what only a person can judge gets click-by-click steps, and only you can record the result, even from your phone while the session is busy | `UserPromptExpansion` (Claude Code) or `UserPromptSubmit` (Codex) records `verify` commands you type; messages queued while the session works are read from the transcript; agents are refused |
| **Change the plan mid-run**: `/daedalus:change <what you want>` is recorded and shown to the session until it's in the plan; "build it last" becomes an ordering rule | the same hooks record it; `SessionStart` and the build-all nudge show open changes |
| **What's live gets checked where it's served**: a deploy runs live checks, and the status says which commit is live | `dd.py deploy`; `PreToolUse` refuses the deploy command run directly |
| **Roles in separate contexts**: the main session orchestrates; a builder, a test writer, a reviewer and a researcher each work in their own context, with their own permissions | `PreToolUse` checks the acting agent on every tool call; `SubagentStart` tells each agent its role |
| **No commit without an independent review** of exactly that change | the commit hook checks an approving verdict whose fingerprint matches the current change set; only the reviewer can record one, and any later edit voids it |
| **Tests can't be gamed by the builder**: no edits to tests or test config by any route, and a legitimate `BLOCKED` exit when the spec can't be met | `PreToolUse` role rules (editor tools and shell writes); agent instructions |
| **Requirements pass only through the gates**, and a milestone is done only with end-to-end evidence | the commit hook refuses uncovered requirements and records pass/fail; no agent can write `.daedalus/gates/` or run `verify-manual` |
| **The guards fail closed**: a crashing hook or a broken config blocks the action; gates run within a time budget so the hook decides before the host's timeout would let the action through | error handling in `dd.py`; `HOOK_GATE_BUDGET`; in Claude Code also `onFailure: "block"`, so a hook that can't even start (no Python) blocks too |
| **Shell commands are judged by what they do**: what a command writes and reads, not the words in it, so probing in `/tmp`, inline scripts and heredocs don't trip the guards, and writing a guarded file by any route still does | `dd.py` splits each command like a shell (quotes, heredocs, redirects, `cd`, variables) and reads inline code for write calls |

The logic is one standard-library Python script, `scripts/dd.py`, so it behaves the same every time.
Projects without a `.daedalus/config.json` are left alone: the plugin can stay enabled everywhere.

## Install

**Claude Code** (2.1.295 or newer):

```bash
claude plugin marketplace add ciprianrosca/daedalus
claude plugin install daedalus@daedalus
```

**Codex CLI** (0.160 or newer):

```bash
codex plugin marketplace add ciprianrosca/daedalus
codex plugin add daedalus@daedalus
```

Then, in Codex: open `/hooks` and trust the Daedalus hooks (Codex skips hooks you haven't trusted, and
asks again after each update), and trust each project folder when Codex asks (its `.codex/` settings
load only then).

**Updates:** `claude plugin marketplace update daedalus` (or the `/plugin` menu) in Claude Code;
`codex plugin marketplace upgrade` in Codex, then trust the updated hooks in `/hooks`.

**From a local clone** (to work on Daedalus itself), use the clone's folder instead of
`ciprianrosca/daedalus` in the commands above. Claude Code then loads the plugin in place: after
editing it, run `/reload-plugins` or start a new session. To try it for one session without
installing: `claude --plugin-dir <path to the clone>`. Codex copies the plugin into its cache, so run
`codex plugin add daedalus@daedalus` again after each edit.

Both need Python 3.10+ and Git (Git Bash runs the gate commands on Windows). In Claude Code the hooks
start through `scripts/dd`, a small launcher that finds Python as `python3`, `python` or `py -3`. Codex
starts them with `python`, so there the command must exist (`sudo apt install python-is-python3` on
Debian and Ubuntu); `dd.py doctor` checks it.

## Use

In a project's repo root (in Codex, `$daedalus:<name>` instead of `/daedalus:<name>`):

```
/daedalus:init          set up the project: detects the checks, asks three plain questions, writes a plan
/daedalus:init docs/spec.md   the same, taking the answers from a spec
/daedalus:start         where we are / next / blocked (runs automatically at session start)
/daedalus:build next    next step: brief → test writer → builder → reviewer → gates → commit
/daedalus:build M2      keep going until milestone 2's gate passes or a step is blocked
/daedalus:build all     build every milestone in the plan, one after another; stop only for what needs you
/daedalus:try           what to try yourself, step by step (you record it with /daedalus:verify)
/daedalus:verify UI-3 ok    record a check only you can make (or: fail <what happened>; all ok; M3 ok)
/daedalus:change <what you want>   change what gets built, at any time, also during a run
/daedalus:gate          run the gates (--all includes manual ones; --accept sets a new baseline)
/daedalus:decide <item> options and a recommendation for an open decision
/daedalus:save          gates, status, commit, handoff, board, learnings
/daedalus:pause         stop for the day: save everything and hand over
/daedalus:status · /daedalus:todo · /daedalus:learn
```

`dd.py deploy` deploys the last commit and runs the live checks (see Deploys below).

`python <daedalus>/scripts/dd.py doctor` checks that everything works in a project, in plain language,
with the fix for each problem.

## What a project gets

```
.daedalus/
  config.json     gates, protected paths, push/stop policies
  plan.md         milestones, each with a measurable gate
  status.md       latest results + Next (or point status_file at an existing file)
  TODO.md         decisions and human tasks
  state/          session handoffs (YYYY-MM-DD-slug.md) and automatic snapshots (-auto-)
  memory/         decisions and corrections
  knowledge.json  technical lessons
  gates/          baseline.json, requirements.json and deployed.json (committed; harness-written);
                  last.json, history.jsonl and passed.json (local)
  changes.json    the user's changes to what gets built, and where each went in the plan
  reviews/        reviewer verdicts by change fingerprint (committed)
  research/       researcher notes
  specs/          one spec per step: requirement IDs and how each is verified
.claude/settings.json   Claude Code compacts at the limit (CLAUDE_AUTOCOMPACT_PCT_OVERRIDE)
.codex/                 if Codex is installed: config.toml (compaction; git access from the Windows
                        sandbox) and agents/daedalus-*.toml (the four roles)
AGENTS.md               a Daedalus section telling Codex what to do if the hooks aren't running
```

`/daedalus:init` runs `dd.py init --auto`, which reads the project's own files (package.json,
pyproject.toml, go.mod, Cargo.toml, Playwright or Cypress config, lockfiles) and proposes the gates, the
source and test folders and the protected secret files, so the user is asked only what only they know:
what they're building, how they'd check it works, and what must never change. A project without tests
or an end-to-end check starts with a milestone that adds them.

### Roles

In `orchestrated` mode (the default) the main session never edits source or tests; it briefs
agents and keeps only their short reports. Each agent starts with an empty context.

| Agent | Does | The hooks refuse |
|---|---|---|
| main session (orchestrator) | picks the step, writes the brief, spawns agents, commits | edits to `roles.source` and `roles.tests` |
| `daedalus:test-writer` | writes the step's tests from the spec and the interface | reading or searching source (Read, Grep, shell), writing anything but tests |
| `daedalus:builder` | implements until the tests and gates pass | editing tests |
| `daedalus:reviewer` | reviews the change set, records a verdict | editing anything but its verdict file |
| `daedalus:researcher` | answers one question with sources | editing anything but `.daedalus/research/` |
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
be committed. The reviewer writes `.daedalus/reviews/draft.json` and runs `dd.py review-record`,
which validates it (every finding needs file, line and severity; an approval needs a `checked` list
and no blocker or major findings) and stamps it with a fingerprint of the exact files. Commit the
verdict with the change: it's the audit trail. `dd.py review-status` shows where a change stands.

### Requirements and milestones

Each build step gets a spec, `.daedalus/specs/<step>.json`, with requirement IDs in testable form and
how each is verified: `test`, `e2e` (the real program, run by a gate with `"evidence": "e2e"`) or
`manual` (only you can check it, with `dd.py verify-manual ID --note "…"`; agents are refused).

- Tests name what they cover: `# covers: STATS-1`. A commit is refused while an active requirement has
  no test naming it.
- The commit hook records which requirements pass in `.daedalus/gates/requirements.json` when a commit
  goes through, and so does a gate run on a clean tree: the results describe committed code, never
  half-finished edits. An entry changes only when its result does. Agents can't write that folder, and
  the status file can't overrule it.
- `dd.py milestone M1` says whether M1 is done: every requirement passes and, by default, at least one is
  proven end to end. `dd.py requirements` lists coverage and results.

Why: in [ImpossibleBench](https://arxiv.org/abs/2510.20270) frontier models cheated on 50–76% of
impossible tasks, Claude mostly by editing tests despite being told not to; read-only tests and an
explicit way out cut that sharply. Verification failures and premature "done" are among the most common
multi-agent failures ([MAST](https://arxiv.org/html/2503.13657v3);
[Anthropic](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)).

### Build everything

Hand Daedalus a full spec (link it at the top of `plan.md`, or give it to `/daedalus:init`) and run
`/daedalus:build all`. It works through the milestones in plan order and doesn't stop between them:

- a milestone whose gate passes is saved, and the next one starts;
- a milestone that waits only on your manual checks is left for you, and the build moves on (under
  `"milestones": "strict"` the run stops there instead);
- a change you send with `/daedalus:change` (also from your phone while the session is busy) is folded
  into the plan before the next step; "build this last" or "after What-if" becomes an `After: all` or
  `After: M8` line on the milestone, and `dd.py run` lists the milestones in that order and names the next;
- a step that needs your decision goes on the board with options and a recommendation, and the build
  carries on with the steps that don't depend on it;
- a step that stays blocked (an impossible spec, review rounds used up) goes on the board the same way.

`dd.py run start` marks the run active in `.daedalus/run.json`. While it is, the `Stop` hook sends the
session back to work each time it tries to end its turn, as long as it has committed something since
the hook's last nudge. A run that tries to stop twice without a new commit ends itself, so it can't trap
the session. The run survives compaction, and after a pause `/daedalus:start continue` picks it up
again. It ends with `dd.py run end --reason "…"` once every milestone is done or everything left waits
on you (the run then ends "waiting on you", not "stopped"), and the final report lists what you need
to do: decisions, checks to try, blocked steps.
Nothing is pushed. `dd.py run` shows where it stands.

### Context budget

```json
"context": {"warn_pct": 55, "limit_pct": 65, "window": "auto", "on_limit": "compact"}
```

The harness reads the session's token use from its transcript on every tool call: Claude Code's
transcript (the same number the status line shows as used), or Codex's rollout (`last_token_usage`
against `model_context_window`). In Claude Code the window follows the documented defaults: 1M for
Opus 4.7+, Sonnet 5+, Haiku 5.5+ and Fable models, 200k for older ones unless they run as `[1m]`.
Set `window` (tokens), or the env var `DAEDALUS_CONTEXT_WINDOW`, to override it.

- **At `warn_pct`** the agent is told to finish the current step, commit it if the gates pass and write
  a handoff, so the saved state is complete.
- **At `limit_pct`** the host compacts the conversation itself and the agent carries on. `dd.py setup`
  sets this up: `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` in `.claude/settings.json` (it applies to subagents
  too), and `model_auto_compact_token_limit` (65% of your Codex model's window) plus
  `model_post_turn_compact_threshold_percent` in `.codex/config.toml`. Before compacting, a hook
  writes a mechanical snapshot. After it, the session-start hook loads the saved state again. Each
  compaction is logged to `.daedalus/compactions.jsonl`.
- **Backstop:** if the context still passes `pause_pct` (default `limit_pct + 10`), compaction didn't
  happen (auto-compaction switched off, or the setting missing), and the old pause applies: only saving
  is allowed (`.daedalus/`, the status file, the board, git, the Daedalus CLI), then `/clear` and
  `/daedalus:start continue`. `"on_limit": "pause"` makes that the rule at `limit_pct` itself.

Refusals are logged to `.daedalus/hooks.log`. `python scripts/dd.py context <transcript.jsonl>`
shows a Claude Code session's current use.

### Try it yourself

Some requirements only a person can judge: does it look right, is it fast enough on their phone, does
the flow make sense. They're `"verify": "manual"` in the spec, with steps someone who doesn't code can
follow:

```json
{"id": "UI-3", "text": "…", "verify": "manual",
 "try": {"title": "Sign-up works on a phone", "steps": ["Open http://localhost:3000/signup", "Sign up"],
         "expect": "You land on your dashboard and see your name"}}
```

`/daedalus:try` shows the checklist and starts the app if the steps need it. You record what you saw:

- in Claude Code, `/daedalus:verify UI-3 ok` or `/daedalus:verify UI-3 fail <what happened>`. The
  `UserPromptExpansion` hook records it: it fires only for a command a person typed, and the model
  can't invoke that command. Sent while the session is busy (from your phone over Remote Control, say),
  Claude Code folds the message into the running turn without any hook; the hooks then find it in the
  session's transcript, where Claude Code marks it as typed by a person, and record it once. Nobody may
  write the transcripts. Several checks fit in one message, one per line, and `all ok` (or `M3 ok`)
  records every check still waiting.
- in Codex, `$daedalus:verify UI-3 ok`. The `UserPromptSubmit` hook records it: in Codex it fires only
  for a person's input (subagents talk through tool calls, and a subagent's turn carries its id).
- in any terminal, `python dd.py try --ask`, which walks you through each check.

Agents are refused `verify-manual`, `try --ask` and starting Claude Code or Codex sessions from the
shell (a nested session could type the command for them). A failed check goes on the board and back
through the build loop.

### Codex

The same `dd.py` runs Codex's hooks with `--host codex` (`hooks/codex-hooks.json`, loaded through
`.codex-plugin/plugin.json`). What differs:

- **Edits** arrive as `apply_patch`: each file in the patch is checked like an edit, and a patch whose
  files can't be read is refused.
- **Roles** are Codex agents, `daedalus-builder`, `daedalus-test-writer`, `daedalus-reviewer` and
  `daedalus-researcher`. `dd.py setup` writes them to `.codex/agents/` from `agents/*.md`. Codex's hooks
  name the acting agent, so the same role rules apply.
- **No "ask"**: Codex hooks can only allow or refuse, and an unknown answer counts as a failed hook,
  which lets the action through. So a push or a harness config change is refused, the agent asks you in
  the chat, and your own "yes" (seen by `UserPromptSubmit`) allows that exact action once.
- **Decisions are JSON on stdout** with exit 0: Codex runs hooks through PowerShell on Windows, which
  turns exit code 2 into 1.
- **Windows sandbox:** Codex's elevated sandbox runs commands as another user, and git refuses a
  repository owned by someone else. `dd.py setup` adds this folder as a git `safe.directory` for the
  commands Codex runs here, through `.codex/config.toml`, so commits work.
- **Limits:** Codex lets an action through when a hook can't start (no Python) or times out, and runs
  hooks only once you've trusted them. `dd.py doctor` checks the setup, and `AGENTS.md` tells Codex to
  warn you when the Daedalus state is missing from its context, which means the hooks aren't running.

### Gates

```json
{"name": "tests", "run": "uv run pytest -q", "when": ["commit"], "timeout": 900}
{"name": "accuracy", "run": "uv run app harness --json", "when": ["commit"], "paths": ["src/geometry/**"],
 "metrics": {"p95_mm": {"max": 1.5}, "median_mm": {"max": 1.0, "tolerance": 0.02}}}
```

- `run` is a shell command run from the project root (Git Bash on Windows). Exit 0 = pass.
- `metrics`: the command prints one JSON line with numbers as its last output line. Each metric has
  `max` or `min`, and by default must not get worse than the accepted baseline (`no_worse`, with an
  optional `tolerance`). `dd.py gate <name> --accept` stores a new baseline.
- `paths`: run the gate only when matching files changed. `when: ["manual"]`: only on `/daedalus:gate --all`.
- A gate that times out fails. Keep the commit gates' total time well under the hook's 60 minutes.
- **One run at a time, and never twice for the same files.** Gate runs in a project take turns (two
  browser suites on one test database collide). A gate that passed on exactly the current files (the
  harness's own records aside) isn't run again: the builder's or the reviewer's passing run counts for
  the commit. `dd.py gate --fresh` runs everything anyway; `"reuse_passing_runs": false` turns it off.
- **Commits with nothing to check skip the gates**: the setup commit, and commits of only the harness's
  records (status, board, specs, reviews) or of `no_gates_for` paths (`["docs/**"]` in new projects).
- **Loosening needs you.** A commit whose config removes or changes a gate, unprotects a path, or turns a
  check off asks for your OK, however the file was changed. Adding checks doesn't.

### Deploys

The gates check the repository. What a deploy serves can still differ (cached assets, data built by the
old code), so a project that deploys says how, and how to check the result where it's served:

```json
"deploy": {"run": "bash deploy/deploy.sh", "timeout": 1200},
"gates": [{"name": "live", "run": "node --test tests/live/", "when": ["deploy"], "evidence": "e2e", "tests": ["tests/live/**"]}]
```

`dd.py deploy` deploys the last commit (it refuses uncommitted work), then runs the `deploy` gates and
records which commit is live and whether its checks passed (`.daedalus/gates/deployed.json`). Session
start, `dd.py run` and the final report say what's live and how many commits aren't. Agents deploy only
this way: running the deploy command directly is refused, so the live checks always follow a deploy. A
requirement proven by a live test (`covers: LIVE-1`) passes only through a deploy.

## Develop

`python -m unittest discover -s tests` drives every hook the way Claude Code and Codex do (JSON on
stdin) against throwaway git repos. `claude plugin validate .` checks the Claude Code manifests.

## License

MIT, see [LICENSE](LICENSE).
