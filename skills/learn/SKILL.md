---
name: learn
description: "Regista's learning loop: record technical lessons, decisions and the user's corrections so the next session builds faster. Use after a gate run that taught something, an environment or library problem, a decision by the user, or a correction from the user."
---

# Learn: one fact, one home

| Learning | Where |
|---|---|
| A technique, quirk, library or environment lesson | `.regista/knowledge.json → patterns.<area>` (dated, one sentence each) |
| A decision by the user | `.regista/memory/decision_<topic>.md`, and tick it on the board |
| A correction to how Regista works | `.regista/memory/feedback_<topic>.md` |
| Results | the status file, not memory |

Memory files have frontmatter (`name`, `description`, `metadata.type`: decision, feedback or
reference), then the fact, **Why:** and **How to apply:**. Add one line per file to
`.regista/memory/MEMORY.md`.

Verify before recording. Absolute dates. Update instead of duplicating. Never store secrets. These
files belong to the project and are committed with the next checkpoint.
