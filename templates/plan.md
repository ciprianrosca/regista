# {{PROJECT}}: plan

The spec Regista builds against. Milestones run in order; a milestone starts only when the previous
one's gate is met. A line `After: M3` (or `After: all`) under a milestone's heading builds it later.
A line `Files: src/pages/**, tests/test_pages.py` names the files the milestone changes, so the
overseer is warned when two workers would edit the same ones.
Keep each step small enough to finish, test and commit in one sitting. The commit hook ticks a step
when its commit goes through (the step's spec names it), and `regista.py plan` adds milestones and
ticks steps by hand.

## Goal
<one paragraph: what is being built, for whom, and what "done" means>

## M1 — <name>
Files: <the files or folders this milestone changes, as globs>

Gate (acceptance criteria, measurable):
- <criterion, with the number and how it's measured>

Steps:
- [ ] <step>
- [ ] <step>

## M2 — <name>
Gate:
- <criterion>

Steps:
- [ ] <step>

## Out of scope
- <what we are deliberately not building yet>
