---
name: triage
description: Process E2E reports, completed brainstorms, and audit reports into backlog tasks. Routes issues by type (orchestrator/template/meta/infra).
disable-model-invocation: true
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "[--source e2e|brainstorms|audit|all]"
---

# Triage

Process incoming reports and convert findings into backlog tasks.

## Input

- `--source e2e` — only process E2E reports
- `--source brainstorms` — only process brainstorms
- `--source audit` — only process audit reports
- `--source all` or no arguments — process all sources

## Sources

### 1. E2E Reports (`docs/e2e_results/`)

Read all `.md` files in `docs/e2e_results/`, **excluding `*-worker.md`** (raw worker audits — their findings are already included in the main report). For each report, find the `## Problems Found` section.

For each problem with structured fields:
- **Severity**: critical / major / minor / info
- **Type**: orchestrator / template / meta / infra
- **Backlog**: `#XX` (existing), `new` (create), or `—` (skip)

Only process problems where `Backlog: new`.

After creating a backlog task, **update the report file**: change `Backlog: new` → `Backlog: #XX` (the created task ID). This marks the problem as processed.

If a report has no structured Problems section (old format), scan for issues manually, classify them, and **rewrite the Problems section** in structured format with appropriate `Backlog` values.

### 2. Brainstorms (`docs/brainstorms/`)

Read all `.md` files. Only process files with `> **Status**: done` in the header.

Find the `## Action Items` section. For each item:
- `→ backlog #XX` — already in backlog, skip
- `→ idea: "..."` — add to Ideas section in backlog
- `→ new task: "..."` — create task in Queue

After processing, update the brainstorm's Status to `triaged`.

### 3. Audit report (`docs/audit.md`)

If exists, read and extract actionable items not already in backlog.

## Routing by Type

| Type | Action |
|------|--------|
| `orchestrator` | Add to `docs/backlog.md` Queue with appropriate priority |
| `template` | Add to `/home/vlad/projects/service-template/docs/backlog.md` (create file if missing, free format). Stage and commit in that repo: `git -C /home/vlad/projects/service-template add docs/backlog.md && git -C /home/vlad/projects/service-template commit -m "docs: add triaged tasks from orchestrator E2E"` |
| `meta` | Add to `docs/backlog.md` Queue with `[meta]` prefix in title |
| `infra` | Don't create tasks. Collect and list at the end for human decision. |

## Regression Detection

Before creating a new task, check `Done` section in backlog for matches:
- Search by keywords, affected service, error pattern
- If a problem matches a completed task `#X`:
  1. **Reopen** — move `#X` back from Done to Queue, set `Priority: critical`, add `[regression]` prefix
  2. **Attach context** — add to Brief: link to E2E report, what was expected to fix it, why it likely didn't work
  3. **Restore plan** — if `docs/plans/<task>.md` still exists, reference it. If deleted, recover from git: `git show main:docs/plans/<task>.md`
  4. Do NOT create a duplicate task

## Deduplication

Before creating any task, check existing backlog Queue and Ideas for duplicates:
- Search by keywords from the problem description
- If a matching task exists, skip (or update its Brief if new info is available)

## Adding to Backlog

New Queue items use this format:

```markdown
### #<next_id> <Title>
- **Priority**: <HIGH|MEDIUM based on severity: critical/major→HIGH, minor→MEDIUM>
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: <Description from the report/brainstorm>
```

Place after existing items of the same priority.

Next ID = max existing ID in backlog + 1.

## Commit

After all processing, commit changed files:

```bash
git add docs/backlog.md docs/e2e_results/ docs/brainstorms/
git commit -m "triage: <N> tasks created, <M> reports processed"
```

If template backlog was updated, also commit in that repo:
```bash
git -C /home/vlad/projects/service-template add docs/backlog.md
git -C /home/vlad/projects/service-template commit -m "triage: tasks from orchestrator E2E"
```

## Output

Print a summary table:

```
## Triage Report

### Processed
| Source | File | Problems | Tasks Created | Skipped (dup/known) |
|--------|------|----------|---------------|---------------------|
| e2e    | todo_api-20260303-levelC-3.md | 2 | 1 (#23) | 1 (known #22) |
| brainstorm | agent-hierarchy.md | 3 | 0 | 3 (already in backlog) |

### Created Tasks
- #23 Deploy timeout retry logic (HIGH) → backlog.md
- template: POSTGRES_HOST mismatch → service-template/docs/backlog.md

### Needs Human Decision (infra)
- Server disk space monitoring (from E2E report)

### Brainstorms Triaged
- agent-hierarchy.md: done → triaged
```
