---
name: try
description: "Try it yourself: walk the user through checking the built software with their own eyes (start the app, give click-by-click steps and what they should see) and collect the checks only they can make. Use when the user says 'daedalus try', 'let me try it', 'how do I test this', when a milestone waits only on manual checks, or after a step that added a manual requirement."
---

# Try: let the user see it work

The gates prove what can be automated. Some things only a person can judge (does it look right, is it
fast enough on their phone, does the flow make sense). Those are requirements with
`"verify": "manual"`, and only the user can record them. This is how they do it without reading code.

The CLI path is in the session-start context: `python "<dd.py>" …`.

1. **Show the checklist.** Run `python "<dd.py>" try` (or `try M1`, `try UI-3`). It lists each check:
   what to do, what they should see, and whether it's done.
2. **Fill in missing steps.** If a check says "no steps written yet", add a `try` block to its
   requirement in `.daedalus/specs/<step>.json`, in plain language for someone who doesn't code:
   ```json
   "try": {"title": "Sign-up works on a phone",
           "steps": ["Open http://localhost:3000/signup on your phone (same Wi-Fi)", "Sign up with any email"],
           "expect": "You land on your dashboard and see your name at the top"}
   ```
   Concrete URLs, buttons and inputs; one action per step; what they should see, not how it works.
3. **Get it running.** If the steps need the app running, start it yourself in the background
   (`npm run dev`, `uv run app`, …), wait until it answers, and give the user the URL. Mention test
   accounts or sample files they'll need.
4. **Hand over.** Show the steps, then tell them how to report what they saw:
   - in Claude Code: `/daedalus:verify UI-3 ok`, or `/daedalus:verify UI-3 fail <what happened>`
   - in any terminal: `python "<dd.py>" try --ask` walks them through each check and records the answers

   Then stop and wait. Never record a check for them, never ask them to paste a command that records
   it, and never treat "looks good" in chat as a recorded check: the harness records only the two ways
   above.
5. **Afterwards:** a failed check is a bug report. Put it on the board and fix it through the build
   loop. When every requirement of the milestone passes, `dd.py milestone M<n>` says it's done.
