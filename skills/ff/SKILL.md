---
name: ff
description: "Fast-forward: build a feature on a branch of its own (ff/<name>), fast, with the light rules, and let the user try it as a separate copy before anything reaches the project's main branch. Also `land`, `merge`, `drop`, `list`. Use when the user says 'regista ff', 'fast-forward', 'prototype', 'try it on a branch', 'just show me', or wants to see a feature working before the full process."
argument-hint: "<name> <what to build> | try | land | merge <name> | drop <name> | list"
---

# Fast-forward: a feature on a branch, to try it out

The full process (test writer, builder, reviewer, every gate on every commit) is right for code that goes into
the project, and slow for an idea the user wants to try today. A fast-forward branch is that idea, built fast on
`ff/<name>`, in its own folder. The project's main branch isn't touched until the user says so.

All commands are `python3 "<regista>/scripts/regista.py" ff …` (the CLI path is in your session context).

## `/regista:ff <name> <what to build>`: start

1. From the project folder: `regista.py ff start <name> --task "<what to build, in the user's words>"`. It makes
   the branch and its folder (a git worktree under `.regista/worktrees/ff-<name>`) and prints the rules.
2. `cd` into that folder. Work only there: the light rules apply there, the full rules everywhere else.
3. Build it yourself, in small steps, committing as you go: no agents, no specs, no handoffs. Commits run only
   the project's quick checks (`fast_forward.gates` in the config; none by default).
4. As soon as there is something to see, run `regista.py ff try` and give the user the link it prints, with
   one or two lines on what to try. Keep `PROTOTYPE.md` (in the folder) honest about what's faked or missing.
5. Iterate on what the user says. `ff try` again after each change worth seeing.

## `/regista:ff land` and `/regista:ff merge <name>`: keep it

Only when the user wants to keep the feature:

1. `regista.py ff land` in the branch's folder: the branch becomes one uncommitted change on `land/<name>`,
   from the project's current code, in the same folder, and the full rules apply again.
2. Bring it up to standard the normal way, as `/regista:build`'s section on a landed fast-forward change says:
   the requirements as a spec, the test writer, the builder, the review, the gates. The prototype's code is kept
   and fixed where the tests or the review fail it, not rewritten. Drop what was only there for the demo. Commit.
3. `cd` back to the project folder, then `regista.py ff merge <name>`: the commit gates run on the merged code,
   and the branch's folder and branches are cleared.

## `/regista:ff drop <name>` · `list`

`drop` throws the branch and its folder away (from the project folder). `list` shows the branches, what each
is for, and whether it's being tried or landing.

## Rules

- A fast-forward branch never deploys, releases or merges as it is: the hooks refuse it. `ff try` is how the
  user sees it.
- Don't start one for a fix the user needs in the project today: that's the normal build.
- Tell the user, in plain words, where things stand: what to try, what's faked, and what landing would take.
