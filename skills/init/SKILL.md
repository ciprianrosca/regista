---
name: init
description: "Set up the Daedalus harness in the current project with as few questions as possible: detects the stack, the test and end-to-end commands and the source folders, protects secrets, sets the agent host to compact itself at the context limit, and writes a short plan with the user. Use when the user says 'daedalus init', 'set up daedalus', or wants to start using Daedalus on a project."
---

# Init: put a project on the harness

**The Daedalus CLI** is `dd.py` in the plugin's `scripts/` folder, run as `"$(command -v python3 || command -v python)" "${CLAUDE_PLUGIN_ROOT}/scripts/dd.py"`
(whichever of `python3` / `python` is installed; use this form in every command below). (In Codex, if that variable isn't
filled in: `dd.py` is in the plugin's `scripts/` folder, two levels above this skill's folder.)

The user may not be a programmer. Don't ask them about gates, globs, hooks or policies: detect what
you can, choose the defaults, and ask only what only they know. Plain words throughout.

1. **Check.** If `.daedalus/config.json` exists, the project is already set up: offer
   `/daedalus:start`. The working directory should be the project's root folder; if it isn't
   obviously that, ask.
2. **Git.** If the folder isn't a git repository, say: "Daedalus saves every finished step as a
   checkpoint with git, so you can always go back. OK to turn this folder into a git repository?"
   On yes, run `git init` (and make a first commit of what's there, if anything).
3. **Detect and create.** Run `"$(command -v python3 || command -v python)" "${CLAUDE_PLUGIN_ROOT}/scripts/dd.py" init --auto --name "<project name>"`.
   It writes `.daedalus/` with the checks it found (tests, lint, types, end-to-end), the source and test
   folders and protected secret files (`.env`, keys), and sets up the agent hosts: Claude Code's
   compaction (`.claude/settings.json`) and, if Codex is installed, `.codex/` (compaction, the four
   Daedalus agents, git access from Codex's Windows sandbox) plus a short note in `AGENTS.md`. Read its
   JSON output: `detected.notes` says what's missing, `hosts` what was set up.
4. **Ask three things, in one message** (use AskUserQuestion where it fits; otherwise plain text):
   - What are you building, and who is it for? (a sentence or two)
   - How would you check that it works, the first time you try it? (this becomes the end-to-end check
     and the first thing they'll try themselves)
   - Is there anything I must never change: files, data, accounts? (`.env` and keys are already protected)
5. **Protected paths.** Add anything they named to `protected` in `.daedalus/config.json`.
6. **Plan.** Write `.daedalus/plan.md` from their answers, short: the goal in their words, then 2–4
   milestones, each with a gate a person can understand ("signing up works end to end on the real
   site"), at least one end-to-end requirement, and a manual check for anything only they can judge
   (looks, feel, speed on their device).
   - **If `detected.notes` says there are no tests or no end-to-end check, start with
     `M0 — Safety net`:** a test runner with one real test, and one end-to-end check that runs the real
     program the way a user would (Playwright for a web app, a script that runs the command on sample
     input for a CLI). Once M0 exists, add its gates to `config.json` (the user confirms the change).
   - If the real spec lives elsewhere (a doc, an issue tracker), link it at the top of `plan.md`.
7. **Check it works.** Run `"$(command -v python3 || command -v python)" "${CLAUDE_PLUGIN_ROOT}/scripts/dd.py" doctor` and fix what you can.
   Install a missing tool only after the user says yes. If gates exist, run
   `"$(command -v python3 || command -v python)" "${CLAUDE_PLUGIN_ROOT}/scripts/dd.py" gate --all`; for metric gates, `gate <name> --accept`
   once to record the starting numbers. If M0 is planned, failing or missing checks are expected: say so.
8. **Commit** what setup wrote: `.daedalus/`, `.claude/settings.json`, and `.codex/` and `AGENTS.md`
   if they were created (`git add` only the ones that exist), with `git commit -m "chore: set up Daedalus"`.
   Then write the first handoff with `/daedalus:save`.
   **Codex only:** tell the user Codex runs the Daedalus hooks only after they trust them: open `/hooks`
   in Codex and trust them (again after each Daedalus update), and trust the project folder when Codex
   asks. `dd.py doctor` shows what's still missing.
9. **Tell the user, in at most 10 plain lines:**
   - what you found (the kind of project, the language)
   - the checks that run before every checkpoint, in their words ("your tests", "a robot that opens the
     site and signs up")
   - what's protected
   - the milestones
   - that long sessions tidy themselves up (the conversation is compacted automatically at the limit,
     and the work carries on from the saved state: nothing to clear or restart)
   - how they'll try things themselves (`/daedalus:try`, then `/daedalus:verify <ID> ok`; in Codex
     `$daedalus:try` and `$daedalus:verify <ID> ok`)
   - what to say next: `/daedalus:build M0` (or `M1`)
   - one line: "Everything lives in `.daedalus/config.json`; ask me to change any of it."

Advanced settings keep their defaults unless the user asks: `mode` orchestrated (separate builder,
test writer and reviewer), `push` ask, `stop` block, `context` warn 55% and compact at 65%
(`on_limit: "pause"` brings back the old stop-and-clear), `milestones` parallel, reviews for changes
of 15+ lines.
