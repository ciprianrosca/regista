---
name: estimate
description: "Estimate what building a milestone, or the whole plan, will cost before it starts: the share of the Claude plan's weekly limit it should use, how much of the week is left and when it resets, whether it fits before Regista stops, and roughly how long it takes. Use when the user says 'regista estimate', 'how much will this cost', 'will M2 fit in my week', 'can I build everything this week', or before 'build M<n>' or 'build all' starts."
---

# Estimate: what a build will cost before it starts

Building uses the Claude plan's weekly limit. Regista records what each past step cost and turns that
into an estimate, so the user can decide whether to start now, build only part, or wait for the weekly
reset. The CLI path is in the session-start context: `python "${CLAUDE_PLUGIN_ROOT}/scripts/regista.py" …`.

1. **Run it.** `python "${CLAUDE_PLUGIN_ROOT}/scripts/regista.py" estimate` for the next milestone,
   `estimate M<n>` for one milestone (for example `estimate M2`), or `estimate all` for every step left in the
   plan. It always exits 0. Add `--json` only if you need the numbers themselves.
2. **Relay the answer in plain words, cost first.** Start with the expected cost as a share of the week
   ("about 3 to 5% of your week"), then how much of the week is used, what's left before Regista stops the
   build and when the week resets, whether it fits, and roughly how long it takes. Keep the times as the
   command writes them (local time, like "Friday 6 November, 07:00"). Mention the tokens only if the user
   asks for detail.
3. **Say how sure it is.** The command says how many past steps the estimate rests on, from this project
   and from other Regista projects on this machine. With few records, or none (a rough default), say the
   figures are rough. The weekly percentage is for the whole account, so other sessions running at the
   same time use the same week.
4. **Help the user decide.** If it fits, offer to start. If it may not fit, say that Regista finishes the
   step it's on, saves and stops the run when the week reaches the stop level, so nothing is left half
   done, and offer to start anyway or to build one milestone now. If it doesn't fit, suggest building part
   of it now or waiting for the reset. If the usage is unknown, give the cost and say the current use
   couldn't be read. The user decides; don't start a build from here unless they ask.

The stop level and the warning level are `usage.stop_at_weekly_pct` (default 90) and
`usage.warn_at_weekly_pct` (default 75) in `.regista/config.json`. Changing them is the user's call.
