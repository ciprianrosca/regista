---
name: gate
description: "Run the project's Regista gates (tests, checks and metric thresholds) on demand, compare with the baseline, and accept a new baseline when the user agrees. Use when the user says 'regista gate', 'run the gates', 'run the harness', or asks whether the project still passes."
---

# Gate: measure, don't believe

The CLI path is in the session-start context: `python "${CLAUDE_PLUGIN_ROOT}/scripts/regista.py" …`.

- `gate`: the commit gates (what the commit hook runs).
- `gate --all`: every gate, including manual-only ones (slow benchmarks, accuracy harnesses).
- `gate <name>`: one gate.
- `gate <name> --accept`: store this run's passing metrics as the new baseline. Only when the user
  agrees the new numbers are the new floor, usually after an improvement.
- A gate with `when_changed` runs only when the change touches those paths; otherwise it's listed as
  "not needed for this change". That's expected, not a gap.
- An end-to-end gate with `run_some` may run only some of its test files at a commit (the ones for the
  milestone being built); its line says "only 3 of its 40 test files". `gate` always runs the whole suite.
- When a run goes over the project's time budget (`gates_budget_seconds`, or a gate's `budget_seconds`),
  the output says so: tell the user, and suggest they run `regista.py tests cleanup`. Don't run it yourself.
- **Long checks run elsewhere** (`"kind": "detached"`): `gate <name> --start` starts one on the last commit
  (`--ref <commit>` for another) and returns at once; `gate --collect` asks for the results; `gate <name>`
  says whether a pass holds for the current code. Never wait for one in a loop: start it, carry on, and
  collect it later (session start checks too). A milestone or a release that needs it waits for its pass.
- **Releases**: `release --check` says what a release would do and what's missing. `release` makes a patch
  release; `release minor` or `major` only when the user asked for it (the hook asks them anyway). It
  needs the changelog entry for the new version, dated today, in plain words for the people who use it.
  If the gates are slow, run it in the background.

Run a gate's command only this way: running it yourself (`npm run e2e`) is refused, because it would run
outside the lock that keeps two test runs from sharing a database or ports. On a folder with no
uncommitted work, the run records which requirements pass and commits that record on its own (a commit of
the harness's records, which skips the gates), so the folder stays clean.

Report each gate's pass/fail and time; for metric gates, the numbers against thresholds and the
baseline; for failures, the cause from the output tail. Runs are logged in `.regista/gates/last.json`
and `history.jsonl`. Put headline numbers in the status file under "Latest results", dated, with how
they were measured.

Never redefine a metric, loosen a threshold or edit the baseline to make a gate pass. If a gate is
wrong, say so and propose the change; the user edits `config.json`.
