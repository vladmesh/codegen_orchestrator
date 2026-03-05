---
name: triage
description: Process E2E reports, completed brainstorms, and audit reports into backlog tasks. Routes issues by type (orchestrator/template/meta/infra). Reorders backlog Queue when roadmap changes.
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

Read all `.md` files in `docs/e2e_results/`. For each report, find the `## Problems Found` section.

For each problem with structured fields:
- **Severity**: critical / major / minor / info
- **Type**: orchestrator / template / meta / infra
- **Backlog**: `#XX` (existing), `new` (create), or `—` (skip)

Only process problems where `Backlog: new` or `Backlog: template`.

- `Backlog: new` → create task in orchestrator or template backlog (based on Type), update to `Backlog: #XX`.
- `Backlog: template` → route to service-template backlog (see Routing by Type), update to `Backlog: template (triaged)`.

After creating a backlog task, **update the report file** to mark the problem as processed.

If a report has no structured Problems section (old format), scan for issues manually, classify them, and **rewrite the Problems section** in structured format with appropriate `Backlog` values.

### 2. Brainstorms (`docs/brainstorms/`)

Read all `.md` files. **Only** process files with `> **Status**: done` in the header.
Skip `draft` brainstorms even if they have Action Items — the brainstorm may still
be in progress and action items may change.

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

## Blocked/Regression Resolution

After processing new reports, check Queue for tasks with `blocked` status or
`[regression]` prefix. For each such task, search new E2E reports for evidence
that the problem is resolved (e.g., smoke_result now passes, deploy succeeds,
specific error no longer appears).

If resolved:
1. Move the task to Done with note: "Confirmed resolved in `<report filename>`"
2. Report in triage summary under "### Resolved (confirmed by E2E)"

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

## Roadmap ↔ Backlog Sync

After processing all sources (and before committing), synchronize ROADMAP.md and backlog.md.

### Step 1: Ensure every ROADMAP item has a backlog task

Parse each incomplete (`- [ ]`) item in ROADMAP.md. For each item:
- If it contains `(#XX)` — verify `#XX` exists in backlog Queue. If missing, report as orphan.
- If it has no `(#XX)` — search backlog Queue and Ideas by keywords for a matching task.
  - **Found** → update the ROADMAP line to include `(#XX)`.
  - **Not found** → report in triage output under `### ROADMAP items without backlog tasks`
    so user can decide whether to create a task or defer.

### Step 2: Reorder Queue by phases

Compare ROADMAP.md and backlog.md git modification times:

```bash
git log -1 --format="%H %ai" -- docs/ROADMAP.md
git log -1 --format="%H %ai" -- docs/backlog.md
```

Reorder if ROADMAP.md was modified **more recently** than backlog.md, OR if new tasks
were created in this triage run.

How to reorder:

1. **Parse ROADMAP phases** — identify which tasks (`#ID`) belong to which phase
   (from the `(#XX)` annotations added/verified in Step 1). The earliest incomplete
   phase = "current phase".

2. **Sort Queue**:
   - Tasks in current phase → top of Queue, ordered by:
     1. Priority (HIGH before MEDIUM)
     2. Order within the phase in ROADMAP
   - Tasks in next phase → middle
   - Tasks not in any phase → bottom (keep original relative order)

3. **Adjust priorities to match phase**:
   - Task in current phase but marked MEDIUM → bump to HIGH if the phase is a blocker milestone (e.g., pre-MVP)
   - Task explicitly deferred to future phase (was HIGH) → downgrade to MEDIUM
   - Add a note to Brief when priority changes: `Priority adjusted by triage (roadmap phase change).`

4. **Don't touch**: Status, Plan, User Story, Brief content (except priority note). Only reorder entries and adjust Priority field.

### What to report

If reordering happened, add a section to the output:

```
### Queue Reordered (roadmap changed)
- Moved to top: #27 PO tools pass user_id (Phase 2A)
- Priority HIGH→MEDIUM: #2 Agent Hierarchy (moved to Phase 4)
- No change: #8 Workspace Failure Counter (same phase)
```

If ROADMAP items are missing backlog tasks:

```
### ROADMAP items without backlog tasks
- Phase 2B: "Shared uv-cache isolation — per-project volume" — no matching task
- Phase 3: "Dev pipeline skills refinement" — no matching task
```

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

### Queue Reordered (roadmap changed)
- Moved to top: #27 PO tools pass user_id (Phase 2A)
- Priority HIGH→MEDIUM: #2 Agent Hierarchy (moved to Phase 4)
```

If no reordering was needed, omit this section.
