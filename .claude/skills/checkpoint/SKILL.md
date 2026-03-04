---
name: checkpoint
description: Periodic review — run audit, triage reports, update CHANGELOG/ROADMAP/STATUS, recommend next task. Use every 5-7 tasks or weekly.
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

If the latest run **failed**: fetch failed logs (`gh run view <run-id> --log-failed | tail -50`), write a brief report to `docs/brainstorms/ci-failure-<date>.md` with Status: draft, and include the failure in the checkpoint report (step 11) under a `### CI Status` section. This is a blocker that should be called out prominently.

If the latest run **passed**: note it for the report, no document needed.

Read:
- `docs/STATUS.md` — current task, last checkpoint date
- `docs/backlog.md` — Queue, Ideas, Done
- `docs/CHANGELOG.md` — recent entries
- `docs/ROADMAP.md` — milestone progress
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
- Route issues to appropriate backlogs

### 4. Update CHANGELOG

Check git log since last CHANGELOG entry date. For each commit not already in CHANGELOG:
- Categorize as Added / Changed / Fixed / Removed
- Add under today's date section
- Reference backlog item IDs where applicable

### 5. Update ROADMAP

Read `docs/backlog.md` Done section. For each completed task:
- Find it in ROADMAP.md
- Mark as `[x]`
- If all items in a milestone are done, update milestone status

### 6. Update User Stories

Read `docs/USER_STORIES.md` and `docs/e2e_results/`. For each User Story:
- If there is a **passing** E2E result that covers the story's acceptance criteria — set `Статус: Done`, update E2E field with date and result link
- If blockers are resolved (blocked-by task is in Done) — change `Статус: Blocked` → `Ready` (or `Done` if E2E also passes)
- Do NOT unblock if the blocking task has no passing E2E yet

### 7. Update STATUS

Update `## Last Checkpoint`:
- Set date to today
- Summarize audit and E2E status

### 8. Cleanup plans

For each file in `docs/plans/`:
- Check if the task is in backlog Done section
- Check if there is an E2E result in `docs/e2e_results/` **newer** than the task's completion commit
- If both conditions are true — delete the plan file
- Otherwise — keep it (it may still be needed for debugging)

### 9. Cleanup E2E reports

For each file in `docs/e2e_results/`:
- Skip if file date (from filename) is **less than 2 days old**
- Check if it was processed by triage (problems have `Backlog: #XX` or `Backlog: —`, not `new`)
- If processed AND older than 2 days — delete the report (and its `-worker.md` pair if exists)
- Keep the **latest passing report per scenario** regardless of age (needed for User Story status)

### 10. Commit

```bash
git add docs/CHANGELOG.md docs/ROADMAP.md docs/STATUS.md docs/USER_STORIES.md docs/backlog.md docs/audit.md docs/plans/ docs/e2e_results/ docs/brainstorms/
git commit -m "checkpoint: <date>"
```

### 11. Report

Print a comprehensive summary:

```
## Checkpoint Report — <date>

### CI Status
- ✅ Green / ❌ FAILING — <link to run> (<failure category if broken>)

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
- Template tasks: N

### ROADMAP Progress
- <Milestone>: X/Y complete

### Recommended Next Task
- #<ID> — <Title> (<reason>)

### Open Questions for Human
- <anything that needs human input>
```
