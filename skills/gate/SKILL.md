---
name: gate
description: "Run the project's Daedalus gates (tests, checks and metric thresholds) on demand, compare with the baseline, and accept a new baseline when the user agrees. Use when the user says 'daedalus gate', 'run the gates', 'run the harness', or asks whether the project still passes."
---

# Gate: measure, don't believe

The CLI path is in the session-start context: `python "${CLAUDE_PLUGIN_ROOT}/scripts/dd.py" …`.

- `gate`: the commit gates (what the commit hook runs).
- `gate --all`: every gate, including manual-only ones (slow benchmarks, accuracy harnesses).
- `gate <name>`: one gate.
- `gate <name> --accept`: store this run's passing metrics as the new baseline. Only when the user
  agrees the new numbers are the new floor, usually after an improvement.

Report each gate's pass/fail and time; for metric gates, the numbers against thresholds and the
baseline; for failures, the cause from the output tail. Runs are logged in `.daedalus/gates/last.json`
and `history.jsonl`. Put headline numbers in the status file under "Latest results", dated, with how
they were measured.

Never redefine a metric, loosen a threshold or edit the baseline to make a gate pass. If a gate is
wrong, say so and propose the change; the user edits `config.json`.
