---
name: checkpoint
description: Periodic review — run audit, triage reports, update CHANGELOG/ROADMAP, recommend next task. Use every 5-7 tasks or weekly.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "[--skip-audit]"
---

# Checkpoint

Periodic review of project state. Ensures docs are up-to-date, reports are triaged, and the next task is clear.

## Input

- `--skip-audit` — skip the code audit step (if one was done recently)

## Protocol

### 1. Gather state

**CI Health Check** — before anything else, verify CI is green:

```bash
gh run list --branch main --limit 1 --json status,conclusion,name,headSha,createdAt,url
```

If the latest run **failed**: fetch failed logs (`gh run view <run-id> --log-failed | tail -50`), write a brief report to `docs/brainstorms/ci-failure-<date>.md` with Status: draft, and include the failure in the checkpoint report (step 11) under a `### CI Status` section.

If the latest run **passed**: note it for the report.

**Get stats from backlog:**

Parse `docs/backlog.md`:
- Count tasks under `## Queue` (backlog)
- Count tasks with `Status: in_dev` (in progress)
- Count entries under `## Done`

Read:
- `docs/CHANGELOG.md` — recent entries
- `docs/ROADMAP.md` — story progress
- `git log --oneline` since last checkpoint date

### 2. Audit (unless --skip-audit)

Invoke the `/audit` skill logic:
- Scan for dead code, smells, security issues
- Writes `docs/audit.md` (report only, no backlog changes — `/triage` handles that)

If an audit was done within the last 5 tasks (check `docs/audit.md` date), skip.

### 3. Triage

Invoke the `/triage` skill logic:
- Process untriaged E2E reports
- Process brainstorms with Status: done
- Add new tasks to `docs/backlog.md`

### 4. Update CHANGELOG

Check git log since last CHANGELOG entry date. For each commit not already in CHANGELOG:
- Categorize as Added / Changed / Fixed / Removed
- Add under today's date section
- Reference backlog item IDs where applicable

### 5. Update ROADMAP

Read `docs/backlog.md` Done section. For each completed task:
- Find it in ROADMAP.md
- Mark as `[x]`
- If all tasks in a story are done, note it

### 6. Update User Stories

Read `docs/USER_STORIES.md` and `docs/e2e_results/`. For each User Story:
- If there is a **passing** E2E result that covers the story's acceptance criteria — set `Статус: Done`, update E2E field with date and result link
- If blockers are resolved (blocked-by task is in Done) — change `Статус: Blocked` → `Ready` (or `Done` if E2E also passes)
- Do NOT unblock if the blocking task has no passing E2E yet

### 7. Review project documentation

Check whether key project docs are still accurate after recent changes:
- Read `ARCHITECTURE.md`, `README.md`, `docs/CONTRACTS.md`
- Compare against commits since last checkpoint
- Update any stale sections
- If `CLAUDE.md` has outdated patterns or file paths — update it too
- Skip docs that are already accurate

### 8. Cleanup plans

For each file in `docs/plans/`:
- Extract tag from filename
- Check if `#<tag>` appears in `## Done` section of `docs/backlog.md`
- If done — delete the plan file

### 9. Cleanup E2E reports

For each file in `docs/e2e_results/`:
- Skip if file date is less than 2 days old
- Check if processed by triage (problems have `Backlog: #XX` or `Backlog: —`, not `new`)
- If processed AND older than 2 days — delete
- Keep the latest passing report per scenario

### 10. Commit (DO NOT push — doc-only commits stay local to avoid wasting CI minutes)

```bash
git add docs/CHANGELOG.md docs/ROADMAP.md docs/USER_STORIES.md docs/backlog.md docs/audit.md docs/plans/ docs/e2e_results/ docs/brainstorms/ docs/ideas.md ARCHITECTURE.md README.md CLAUDE.md docs/CONTRACTS.md
git commit -m "checkpoint: <date>"
```

### 11. Report

Print a comprehensive summary:

```
## Checkpoint Report — <date>

### CI Status
- ✅ Green / ❌ FAILING — <link to run> (<failure category if broken>)

### Backlog Stats
- Queue: N | In Dev: N | Done: N

### Since Last Checkpoint (<previous date>)
- Tasks completed: #X, #Y, #Z
- Commits: N
- E2E runs: N (pass/fail)

### Audit Summary
- Critical: 0
- New backlog items: N

### Triage Summary
- E2E reports processed: N
- Brainstorms triaged: N
- Tasks created: N

### ROADMAP Progress
- <Story>: X/Y tasks complete

### Recommended Next Task
- #<ID> — <Title> (<reason>)

### Open Questions for Human
- <anything that needs human input>
```

### Memory Review (Mandatory)

**Before generating your final response, review your memory for feedback:**
Did you have to fix any unexpected errors, correct wrong commands, or guess missing information during this task?
If yes, you **MUST** append an entry to `docs/skill-feedback.md` right now, following the format described in the **Self-Feedback** section below.

## Self-Feedback

During your final memory review, if you encountered any of the following — add an entry to `docs/skill-feedback.md`:

- A command or path in this skill was **wrong or outdated**
- A step was **missing context** that you had to figure out yourself
- A step could be **simplified or reordered** for better flow
- The skill **gave ambiguous instructions** that led to a wrong first attempt

Entry format:

```markdown
## [checkpoint] — <today's date>
- **Type**: bug | missing-info | optimization
- **Quote**: "<exact line or section from this skill>"
- **Problem**: <what went wrong or was missing>
- **Suggested fix**: <concrete change to the skill text>
```

Only write feedback that is **specific and actionable**. Skip vague impressions.
