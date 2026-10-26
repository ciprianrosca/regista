---
name: change
description: "The user changes what gets built, at any time, including during a build-all run: `/daedalus:change <what you want>`. Typed by the user only; the harness records it and shows it until it's in the plan."
argument-hint: "<what you want built, changed or dropped; ordering like \"build it last\" or \"after M8\" is fine>"
disable-model-invocation: true
---

# Change: the user's change to the plan

The user just typed this command (or sent it while you were busy). The Daedalus hook has already recorded
it in `.daedalus/changes.json` and its message, with the change's number, is in your context. Until you
mark it as planned, session start and every build-all nudge show it.

1. **Fold it into the plan now**, before the next step:
   - new work becomes a milestone in `.daedalus/plan.md` (with a gate and at least one end-to-end
     requirement) or steps in an existing one, plus its spec when the step starts;
   - something already built that should work differently: update the spec first, then the tests, then
     the code, through the usual loop;
   - something dropped: take it out of the plan, and say what that means for anything already built.
2. **Ordering** ("build this last", "after What-if") goes on the milestone as a line of its own:
   `After: M8` or `After: all`. `dd.py run` then lists the milestones in build order and names the next one.
3. **If it contradicts** something already built or decided, or you can't tell what they want, ask one
   short question before planning it.
4. **Mark it planned:** `python "<dd.py>" change done <n> --where "M9"`. Then confirm to the user in one
   line where it went, and carry on.

Only the user adds changes. If you think the plan needs one, ask them.
