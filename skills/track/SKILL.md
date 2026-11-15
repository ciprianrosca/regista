---
name: track
description: "Keep the project's work tracked in its GitHub Project, best effort: find the Project linked to the repository (or offer to create one), learn the format it already uses, and keep its issues in step with the plan, the board and each save. Use at /regista:init, at every /regista:save, when the user says 'regista track', or when they ask to update the GitHub Project, the issues or the project board."
argument-hint: "[setup | sync | off]"
---

# Track: the plan's work, mirrored in the GitHub Project

Regista's own record stays in `.regista/` (plan, status, board, handoffs). When the repository is on
GitHub, this skill keeps a GitHub Project in step with it, so the user can follow the work from GitHub
on any device. It is **best effort**: a missing tool, login or permission is one plain line in the
reply and in the handoff, never a reason to stop a build or a save.

The setting lives in `.regista/config.json` under `"tracker"`:

```json
"tracker": {
  "kind": "github-project",
  "owner": "<user or organization>",
  "number": 1,
  "url": "https://github.com/users/<owner>/projects/1",
  "repo": "<owner>/<repo>"
}
```

or `"tracker": "off"` when the user doesn't want one. What the Project's format looks like is written in
`.regista/memory/reference_github_project.md` (see "Learn the format").

## Can it run at all?

Check, in this order, and stop at the first failure with one line saying what's missing:
1. `gh --version` works. If not: "Tracking in GitHub is off: the GitHub command-line tool (gh) isn't
   installed."
2. `gh repo view --json nameWithOwner,url,owner` works. If not, the folder isn't a GitHub repository
   or gh isn't logged in (`gh auth login`, which the user runs in their own terminal).
3. `gh auth status` lists the `project` scope. If not, tell the user to run this in their own terminal
   (it shows a one-time code and waits for the browser, so it can't run inside the session):
   `gh auth refresh -h github.com -s project`. Until then, issues still work (they need only `repo`):
   keep issues up to date and skip the Project's fields.

## `/regista:track setup`

Run by `/regista:init` on a GitHub repository, and by `sync` when `"tracker"` is missing.

1. **Find an existing Project.** The ones linked to the repository:
   ```
   gh api graphql -f query='query($o:String!,$r:String!){repository(owner:$o,name:$r){projectsV2(first:20){nodes{number title url closed owner{... on User{login} ... on Organization{login}}}}}}' -f o=<owner> -f r=<repo>
   ```
   Then the owner's other open Projects (`gh project list --owner <owner> --format json`), looking for
   one whose title names this project.
   - Exactly one open linked Project: use it.
   - Several, or one only matched by title: ask which one, naming each with its title and link.
   - A title match that isn't linked: once the user confirms it, link it
     (`gh project link <number> --owner <owner> --repo <owner>/<repo>`).
2. **None found: ask, don't create.** "This repository has no GitHub Project. Should I create one so you
   can follow the plan's milestones and steps on GitHub? It would get a status column (Backlog, Todo,
   In Progress, In Review, Done), one issue per milestone, and an issue for each bug or task that comes
   up. Or keep tracking only in `.regista/`." On no, write `"tracker": "off"` and stop. On yes:
   - `gh project create --owner <owner> --title "<project name>" --format json`, then
     `gh project link <number> --owner <owner> --repo <owner>/<repo>`.
   - Set the Status field's options to Backlog, Todo, In Progress, In Review, Done (GraphQL
     `updateProjectV2Field` with `singleSelectOptions`; the field ID comes from
     `gh project field-list <number> --owner <owner> --format json`).
   - One repository milestone per plan milestone, and one issue per milestone with its goal and gate in
     plain words, added to the Project with its status.
   - Tell the user the one step the API can't do: add a Board view ("+ New view", Board), which groups
     by Status.
3. **Write the setting** into `.regista/config.json` and learn the format (below).

## Learn the format

An existing Project already has a way of working. Copy it; don't impose another. Read:
- the fields and their options: `gh project field-list <number> --owner <owner> --format json`
  (Status, Priority, Estimate, iterations, anything custom);
- about 30 items, open and closed: `gh project item-list <number> --owner <owner> --limit 30 --format json`;
- a few of their issues in full: `gh issue view <n> --json title,body,labels,milestone,assignees,state,stateReason`,
  plus `gh api repos/<owner>/<repo>/issues/<n>/sub_issues` for one that looks like an epic;
- the repository's labels and milestones (`gh label list`, `gh api repos/<owner>/<repo>/milestones?state=all`).

Write down, in `.regista/memory/reference_github_project.md` (frontmatter type `reference`, a line in
`MEMORY.md`), only what the next session needs to follow it:
- which Status means "being built", "built, waiting for the user's check", "done", "won't do";
- whether closed issues match Done (and Won't-do closes as "not planned");
- how titles read, and whether milestones, epics (sub-issues), labels and priority are used and how;
- which labels mark the user's own tasks and decisions (for example "HUMAN", "Decision"), which this skill
  never closes or creates without the user;
- how progress comments read (language, length, what they link to).

Never rename, reorder or delete existing fields, options, labels or milestones. Add a new one only when
the user agrees.

## `/regista:track sync` (also step 6 of every save)

Skip quietly when `"tracker"` is `"off"`. Otherwise, after checking it can run:

1. **What changed this session.** Steps finished or started, milestones whose gate passed, bugs found,
   decisions and tasks added to the board, checks the user recorded with `/regista:verify`. The commits
   (`git log` since the last handoff), the status file and the board say it.
2. **Find their issues.** An issue number in a commit message, branch name, handoff or the board first;
   otherwise search: `gh issue list --state all --search "<words from the title>" --json number,title,state`.
   Before creating anything, make sure no matching issue exists.
3. **Move and comment.** For each issue the session worked on:
   - set the Status the format uses for its state (built and waiting for the user's check is not
     "done"; done means finished and, where the plan needs it, checked by the user). The Project item ID:
     `gh project item-list <number> --owner <owner> --limit 500 --format json` (match by `content.number`), or
     `gh project item-add <number> --owner <owner> --url <issue url> --format json` when it isn't on the
     Project yet. Then `gh project item-edit --id <item> --project-id <project id> --field-id <field id> --single-select-option-id <option id>`;
   - close finished issues the way the format does (`gh issue close <n> --reason completed`, or
     `--reason "not planned"`);
   - add one comment per issue saying what changed and what it means now, in the user's plain language,
     with the commit or pull request it landed in. No session nicknames or logs.
4. **Create what's new**, in the format: a new milestone or step, a bug found, a follow-up. Same title
   style, labels, milestone, parent epic (`gh api -X POST repos/<owner>/<repo>/issues/<parent>/sub_issues -F sub_issue_id=<child's id, not its number>`),
   added to the Project with its Status (and Priority, when the format uses it).
5. **The user's own items.** Issues labelled as the user's tasks or decisions: comment with what the
   session learned, but never close, create or reassign them without the user.
6. **Report** one line in the handoff and the reply: "GitHub Project: moved #12 to In Review, closed #9,
   opened #31" or "GitHub Project: skipped (<reason>)".

## Never

- Delete issues, Project items, fields, labels or milestones.
- Notify people: no `@name` in issues or comments unless the user asked for that mention.
- Put secrets, `.env` values or confidential files in an issue, a comment or an attachment.
- Block a save, a build or a commit because GitHub didn't answer.

## `/regista:track off`

Write `"tracker": "off"` in `.regista/config.json`. `/regista:track setup` turns it back on.
