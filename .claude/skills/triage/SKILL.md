---
name: triage
description: Process E2E reports, completed brainstorms, and audit reports into backlog tasks. Routes issues by type (orchestrator/template/meta/infra). Reorders backlog Queue when roadmap changes.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "[--source e2e|brainstorms|audit|all]"
---

# Triage

Process incoming reports and convert findings into backlog tasks via the Work Items API.

## Input

- `--source e2e` — only process E2E reports
- `--source brainstorms` — only process brainstorms
- `--source audit` — only process audit reports
- `--source all` or no arguments — process all sources

## API Helpers

All task creation/lookup goes through the API:

```bash
API="http://localhost:8000"

# Get next tag number
NEXT_TAG=$(curl -sf "$API/api/work-items/next-tag" | jq -r '.next_tag')

# Create a work item
curl -sf -X POST "$API/api/work-items/" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "codegen-orchestrator",
    "title": "#'"$NEXT_TAG"' <Title>",
    "type": "feature",
    "description": "<Brief>",
    "priority": 1,
    "created_by": "triage"
  }'

# Search existing items (for dedup)
curl -sf "$API/api/work-items/?project_id=codegen-orchestrator" | jq '.[].title'

# Reopen a done item (regression)
curl -sf -X POST "$API/api/work-items/<wi_id>/reopen" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Regression found in E2E", "actor": "triage"}'
```

Priority mapping: `0 = CRITICAL, 1 = HIGH, 2 = MEDIUM, 3 = LOW`

## Sources

### 1. E2E Reports (`docs/e2e_results/`)

Read all `.md` files in `docs/e2e_results/`. For each report, find the `## Problems Found` section.

For each problem with structured fields:
- **Severity**: critical / major / minor / info
- **Type**: orchestrator / template / meta / infra
- **Backlog**: `#XX` (existing), `new` (create), or `—` (skip)

Only process problems where `Backlog: new` or `Backlog: template`.

- `Backlog: new` → create work item via API (based on Type), update report to `Backlog: #XX`.
- `Backlog: template` → route to service-template backlog (see Routing by Type), update to `Backlog: template (triaged)`.

After creating a work item, **update the report file** to mark the problem as processed.

If a report has no structured Problems section (old format), scan for issues manually, classify them, and **rewrite the Problems section** in structured format with appropriate `Backlog` values.

### 2. Brainstorms (`docs/brainstorms/`)

Read all `.md` files. **Only** process files with `> **Status**: done` in the header.
Skip `draft` brainstorms even if they have Action Items.

Find the `## Action Items` section. For each item:
- `→ backlog #XX` — already in backlog, skip
- `→ idea: "..."` — add to `docs/ideas.md`
- `→ new task: "..."` — create work item via API

After processing, update the brainstorm's Status to `triaged`.

### 3. Audit report (`docs/audit.md`)

If exists, read and extract actionable items not already in backlog.
Search existing work items via API before creating new ones.

## Routing by Type

| Type | Action |
|------|--------|
| `orchestrator` | Create work item via API with `project_id: "codegen-orchestrator"` |
| `template` | Add to `/home/vlad/projects/service-template/docs/backlog.md` (create file if missing, free format). Stage and commit in that repo. |
| `meta` | Create work item via API with `[meta]` prefix in title |
| `infra` | Don't create tasks. Collect and list at the end for human decision. |

## Regression Detection

Before creating a new work item, check done items via API:
```bash
curl -sf "$API/api/work-items/?status=done&project_id=codegen-orchestrator"
```

Search by keywords, affected service, error pattern. If a problem matches a completed item:
1. **Reopen** — `POST /api/work-items/<wi_id>/reopen` with reason
2. **Update** — `PATCH /api/work-items/<wi_id>` to add regression context to description
3. Do NOT create a duplicate task

## Deduplication

Before creating any work item, search existing items:
```bash
curl -sf "$API/api/work-items/?project_id=codegen-orchestrator"
```
Search by keywords from the problem description. If a matching item exists, skip (or update its description via PATCH if new info is available).

## Roadmap ↔ Backlog Sync

After processing all sources, synchronize ROADMAP.md with work items.

### Step 1: Ensure every ROADMAP item has a work item

Parse each incomplete (`- [ ]`) item in ROADMAP.md. For each item:
- If it contains `(#XX)` — verify `#XX` exists via `GET /api/work-items/by-tag/XX`. If 404, report as orphan.
- If it has no `(#XX)` — search work items by keywords for a matching task.
  - **Found** → update the ROADMAP line to include `(#XX)`.
  - **Not found** → report in triage output under `### ROADMAP items without backlog tasks`

### Step 2: Reorder Queue by phases

Compare ROADMAP.md and backlog.md git modification times:
```bash
git log -1 --format="%H %ai" -- docs/ROADMAP.md
git log -1 --format="%H %ai" -- docs/backlog.md
```

Reorder if ROADMAP.md was modified more recently than backlog.md, OR if new tasks were created in this triage run.

How to reorder — update priority on work items via API:
```bash
curl -sf -X PATCH "$API/api/work-items/<wi_id>" \
  -H "Content-Type: application/json" \
  -d '{"priority": 1}'
```

Priority adjustments:
- Task in current phase but MEDIUM → bump to HIGH if phase is a blocker milestone
- Task deferred to future phase → downgrade to LOW
- Add note to description when priority changes

## Generate Backlog

After all API changes:
```bash
make backlog
```

## Commit

After all processing, commit changed files:

```bash
git add docs/backlog.md docs/e2e_results/ docs/brainstorms/ docs/ideas.md
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
- #23 Deploy timeout retry logic (HIGH) → API
- template: POSTGRES_HOST mismatch → service-template/docs/backlog.md

### Needs Human Decision (infra)
- Server disk space monitoring (from E2E report)

### Brainstorms Triaged
- agent-hierarchy.md: done → triaged

### Queue Reordered (roadmap changed)
- Priority updated: #27 PO tools pass user_id → HIGH (Phase 2A)
- Priority updated: #2 Agent Hierarchy → LOW (moved to Phase 4)
```

If no reordering was needed, omit this section.
