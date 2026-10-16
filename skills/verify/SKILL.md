---
name: verify
description: "The user records a check only they can make: `/daedalus:verify UI-3 ok` or `/daedalus:verify UI-3 fail <what happened>`. Typed by the user only; the harness records it from the typed command."
argument-hint: "<ID> ok | <ID> fail <what happened>"
disable-model-invocation: true
---

# Verify: the user's own check

The user just typed this command. The Daedalus hook has already read it and recorded the result (or
explained why it couldn't); its message is in your context. You don't record anything yourself, and
you can't: `verify-manual` and `try --ask` are refused for agents.

- **Recorded as verified:** confirm it in one line, then say what's still waiting
  (`python "<dd.py>" try`), or carry on with the build if nothing is.
- **Recorded as not working:** thank them, put it on the board (`.daedalus/TODO.md`), and fix it through
  the build loop: update the spec if the requirement was wrong, otherwise send it to a fresh builder
  with what the user saw. If their report isn't enough to act on, ask one short question.
- **Nothing recorded** (no ok/fail, unknown ID): show them the exact form to type, with the right ID.
