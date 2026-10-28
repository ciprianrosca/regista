# What the first parallel builds taught the harness

Version 0.8.0 added the overseer and parallel workers. Since then they have run twice for real:
- The FPL site: five milestones (M15 to M19) from Monday 26 October 2026, 17:27 UTC, to Wednesday 28 October.
- Regista building its own rename (version 0.9.0): five workers on Tuesday 27 October, 12:17 to 20:13 UTC.

On Wednesday 28 October, FPL and Klotho also moved to the new `.regista/` folder. The lessons below come from
those sessions' transcripts and the harness's own records. Each was checked against the 0.9.0 code, and each one
is still a problem there. They're ranked by value, highest first.

**Built in version 0.10.0** (Wednesday 28 October 2026): all fourteen lessons, as Ciprian chose. Not tested
on Windows, on real Codex or with real tmux. Two known gaps remain: the check that browser tests run inside the
lock doesn't know which folder a command runs in, and running one non-browser gate by name on a clean folder
still marks every requirement checked by tests as passing.

## 1. The FPL overseer ran outside the project, so the harness never reached it

The FPL overseer was started in `~/personal-assistants`, not in `~/fpl`. Its hooks found no project, so in about
23 hours it got no worker news at all. It fell back on old habits:
- 76 reads of the workers' screens, 38 background waits of 20 to 25 minutes each, and 46 `git log` calls;
- 18 messages to workers only to renew a "tell me when you're idle" request.

Ciprian still asked "Status?" or "ETA?" 10 times. Outside the project, the rules didn't apply either: the overseer
recorded four of his phone checks itself and edited the plan with scripts.

By contrast, the Regista build's overseer ran in the project. It read a worker's screen once and got 33 reports
without asking.

**Change:** `fleet spawn` and `fleet merge` refuse to run from a session whose folder isn't the project, and say
how to start one there. The oversee skill says so in its first line.

## 2. Merges failed over small things, and the overseer forced them

Across both builds there were 20 merge attempts. 9 of them failed, and 7 merges were forced.
- **"Busy" (4 times).** The worker had just reported "ready", but its turn hadn't ended yet.
- **The harness's own records.** An untracked results file in the overseer's folder blocked one merge for 41
  minutes, until Ciprian moved it by hand. Before other merges, a gate run had left the results file changed, so
  the overseer made a commit by hand first: 6 such commits for FPL and 2 for Regista. The folder move for FPL was
  refused for the same reason.
- **Plan tick boxes.** Two branches ticked steps in `plan.md`, and the merge stopped on the conflict (about 30
  minutes for FPL).
- **A missing package.** An FPL worker added a solver package, but the main folder didn't have it installed. The
  merge's gates hung until they timed out, and the merge took 37 minutes. After that, the overseer compared the
  package lists by hand before every merge.
- **Gates re-run on the same code.** 6 of FPL's 7 merges ran the full gates again on code the worker had just
  passed: about 62 minutes in all. Each worktree keeps its own memory of passing runs.
- **A worker couldn't bring the main branch's changes into its own branch.** The results file conflicted, and the
  hook refused both ways of resolving it.

**Change:**
- A merge waits for a busy worker to finish its turn (`--when-idle`) instead of failing.
- The harness commits its own records itself when nothing else changed, and sets aside an untracked record the
  branch replaces.
- Tick boxes in `plan.md` merge by keeping the ticks from both sides.
- When the package lock file changed, the merge installs dependencies before the gates.
- Passing gate runs are remembered in the git folder all worktrees share.
- A new `fleet sync` command lets a worker bring in the main branch, with the harness's records merged.

## 3. Gate runs leave the folder unclean

Every gate run rewrites the requirement results file, even when only the kind of evidence changed: NAV-1 flipped
from "checked live" to "checked by end-to-end test" and back, with the same pass. The file is left uncommitted, so
merges and the folder move then refuse to run. This causes most of lesson 2.

**Change:** count a change in a requirement's record only when its pass or fail result changes. A gate run on a
clean folder commits its own record. A commit of records only already skips the gates.

## 4. The shell checks still give false alarms

The hooks refused 93 commands across both builds. About 70 were wrong. Replaying them through 0.9.0, about 30
would still be refused wrongly, including FPL's status save this morning at 09:13. The main causes:
- **Paths ending in a variable.** In a path like `$S/tests/$f` or `for f in *.html`, nothing follows the last
  variable, so the check takes the path to be the whole project folder. That covers 9 of FPL's 13 remaining cases.
- **Text inside scripts.** Inside an inline Python script, any string that looks like a path counts as a write
  once the script writes anything: a time zone (`'Europe/Bucharest'`), or a status note that mentions
  `.regista/`. Text inside triple quotes is data, not code.
- **Commands in other folders.** Commands that work on another repository are judged as if they ran in this one:
  `git commit` in a temporary test repository, or `regista.py` run on a scratch project.
- **Plugin commands.** `claude plugin validate` is refused as "starting a nested session" (17 times).

**Change:** fix each of these, and turn every one of these refused commands into a test, so the checks get more
accurate without getting looser.

## 5. Build-all runs ended as "stalled" while they were waiting on their agents

A build-all run ends when the session stops twice without a new commit. But the orchestrator stops on purpose
while its agents work in the background. This ended the run 13 times in the Regista build and in all 5 FPL
workers, within 3 to 7 minutes of the start. The workers kept going only because their agents' notices woke them.
Two of them had to tell the overseer they hadn't actually paused.

**Change:** record when each agent finishes, and don't count a stop as stalling while one is still running.

## 6. A worker waiting for Ciprian's OK sat stuck, and nobody knew

The Regista M1 worker waited 44 minutes, twice, for permission to change its gates. Ciprian found out only when he
asked the overseer for the plan, and then opened the worker to click "Allow". The first change only added a gate,
which loosens nothing.

**Change:**
- Ask only when the change loosens something. Adding a gate or a protected path goes through.
- In a worker, the question goes to the overseer as a "waiting for your OK" event, and Ciprian's reply can come
  through the overseer.

## 7. Agents ran the end-to-end tests outside the lock and collided

Inside the FPL workers, agents ran the end-to-end suite directly 46 times. That's outside the harness's lock, and
all the workers shared one test database. A review saw 92 tests fail because another worker was resetting the
database at the same moment. Two commits were refused on failures caused by load. In all, about 80 minutes were
lost.

**Change:**
- When an agent runs a command that is one of the project's gates, the hook sends it through the harness, which
  takes the lock.
- `init` proposes a database per worker, named after the worker's slot (`REGISTA_SLOT`).

## 8. Gate commands inherited the session's project folder

The harness's gates pass the session's environment to the commands they run, including the variable that names
the project folder (`CLAUDE_PROJECT_DIR`). Inside the Regista build's commit hook, the harness's own tests then
ran on the real worktree. The result was a 15-minute timeout, a fake change and a restarted run written into the
records, and about 35 minutes lost.

**Change:** gates run without that variable.

## 9. The plan was kept by hand, and the parallel milestones overlapped

- **Plan updates.** The FPL overseer added five milestones and ticked steps by writing `plan.md` with scripts (18
  edits). A tick that went missing made M19 look "not ready", so it was started with `--force`. Ciprian's scope
  changes were plain sentences to the overseer, and `change add` is refused to every agent.
- **Overlapping files.** The Regista overseer said the two milestones built side by side "don't share files", but
  both edited the same test file, the plan and a spec. That led to two test clashes and a requirement listed
  twice, which blocks commits. Sorting it out took about an hour.

**Change:**
- A plan command for the overseer: add a milestone, tick a step.
- The commit hook ticks the step it commits, and workers don't edit `plan.md`.
- The overseer may record a change when the transcript shows Ciprian typed it, the same way queued
  `/regista:verify` messages are read.
- Milestones can name their files, and `fleet spawn` warns when two running workers overlap.

## 10. A requirement passed because a test only mentioned it

NAME-1 showed as covered because an old test checks the text "must look like NAME-1". The worker caught it, not the
harness.

**Change:** count only explicit `covers: ID` markers. A plain mention shows as a warning.

## 11. Session start showed the wrong things

- **Old worker news.** This morning, FPL's session start brought in 82 events (10 KB) about workers that had been
  removed a day earlier. 27 of them were progress only.
- **The wrong latest handoff.** Klotho's "latest handoff" was wrong because the folder move gave every file the
  same timestamp.
- **Too technical.** Ciprian asked Klotho to "simplify the explanation above". The start summary asks for
  milestone codes and dated gate results.

**Change:**
- Drop news about removed workers, and fold progress-only updates into one line.
- Sort handoffs by the date in their file name.
- Write the start summary for a person, in line with the writing guide.

## 12. Ciprian did the switch-overs and opened sessions by hand

- **The rename.** The hand-over was a five-step, 20-minute list.
- **Opening sessions.** On 28 October, auto mode twice refused to start a session or type into one, and `!`
  commands don't work in the web app. Ciprian moved to the laptop, and the new Klotho session stalled on the
  "trust this folder" question.
- **Manual checks.** A plain "ok" or "1. All ok" isn't recorded: it has to be the exact verify command. The rename
  build's last manual check (MIG-9) was never recorded.
- **Deploys.** FPL has live checks but no deploy section, so its deploys were done by hand: 18 uploads.

**Change:**
- A `regista.py open <folder> <name>` command starts a session in tmux with Remote Control and answers the
  trust question. It reuses the worker start code.
- `migrate` prints the plugin-swap commands.
- Every question that asks Ciprian for a check ends with the ready-to-send `/regista:verify` line.
- `doctor` proposes a deploy section when live checks exist, and checks that git has an author.

## 13. For a later round: the way Klotho releases

Before Regista, the Klotho assistant had a "planner" routine:
- builder sessions worked in their own clones and opened pull requests;
- a planner merged them, chose the version, tagged the release and deployed;
- slow checks ran detached on the staging server.

Klotho's setup moved that routine into `.regista/planner/`, because Regista has no place for it. Merging through
pull requests, releases and long checks on another server are a bigger design question than the fixes above.

## 14. The test suites grow faster than the code, and every change runs all of them

FPL has about 42,000 lines of tests for about 19,000 lines of site code: about 1,400 tests for 276 requirements,
so roughly 5 tests per requirement, and about 270 browser tests. A full run took 3.5 minutes on 25 October and
takes 14 minutes now: 2.5 minutes of unit tests and 11.5 of browser tests. Every commit and every merge waits for
it. That slowness is behind much of lessons 2 and 7.

A read-only check of FPL's suite on 28 October found that about 200 to 250 of the 1,223 unit test runs and about
230 to 260 of the 428 browser test runs could go, or move down to unit tests, without losing anything. Most of the
browser repetition is one shared check run once per page (the logo check alone runs 72 times). The check removed
50 candidate unit tests in 6 groups, broke the code they guard on purpose 20 times, and the remaining tests still
caught every break the full set caught. About 35 to 40 browser tests, one per page and per main path, would be
enough, and the browser run would drop from about 11.5 minutes to 2 to 3.

There are three reasons:
- The test writer has no guidance on how many tests to write, or at which level.
- Nothing ever removes overlapping tests.
- A one-line change runs the whole browser suite.

**Change, in the building flow** (these only slow the growth and never remove a test):
- The test writer covers each requirement once, at the cheapest level that proves it. Browser tests are kept for
  what needs a browser. At least one browser test stays for every page and every main path a user takes.
- An ordinary commit runs the unit tests plus the current milestone's browser tests. The full browser suite runs
  when a milestone closes, at a merge and before a deploy, so nothing reaches the main branch or the live site
  without it.
- The project sets a time budget for its gates, and the harness warns when a run goes over it.

**Change, outside the building flow:** a cleanup command that only Ciprian starts. The harness can suggest it when
the gates go over their budget, but never runs it on its own. It lists the overlapping tests and why each could go.
A test may go only when a check proves it adds nothing: the harness deliberately breaks the code that test guards,
and the remaining tests must still fail (mutation testing). Nothing is removed until Ciprian approves. The building
flow never removes tests, because the agents trying to get a commit through shouldn't decide which checks to drop.

## What worked and stays as it is

- Workers reported on their own: 14 messages for FPL and 33 for Regista.
- Ciprian asked about 20 questions while the workers built, and was never kept waiting.
- Remembered gate runs saved 42 runs in the Regista build.
- Reviews caught real bugs: 3 sets of changes requested out of 16 reviews, and a port clash with the live FPL
  service.
- The live check caught a broken snapshot format within minutes of a deploy.
- The builder said BLOCKED on an impossible test instead of bending the code.
- `migrate` moved FPL's 133 files in one commit, with their history kept.
