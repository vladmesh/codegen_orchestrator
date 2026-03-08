---
name: triage
description: Process E2E reports, completed brainstorms, and audit reports into backlog tasks. Routes issues by type (orchestrator/template/meta/infra). Reorders backlog Queue when roadmap changes.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "[--source e2e|brainstorms|audit|all]"
---

# Triage

Process incoming reports and convert findings into backlog tasks via the Tasks API.

## Input

- `--source e2e` — only process E2E reports
- `--source brainstorms` — only process brainstorms
- `--source audit` — only process audit reports
- `--source all` or no arguments — process all sources

## API Helpers

All task creation/lookup goes through the API:

```bash
API="http://localhost:8000"

# Resolve project UUID (first project in the list)
PROJECT_ID=$(curl -sf "$API/api/projects/" | jq -r '.[0].id')

# Load stories for story matching
STORIES=$(curl -sf "$API/api/stories/?project_id=$PROJECT_ID" | jq -r '.[] | select(.status == "created") | "\(.id) | \(.title)"')

# Get next tag number
NEXT_TAG=$(curl -sf "$API/api/tasks/next-tag" | jq -r '.next_tag')

# Create a task (always include story_id!)
curl -sf -X POST "$API/api/tasks/" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "'"$PROJECT_ID"'",
    "title": "#'"$NEXT_TAG"' <Title>",
    "type": "feature",
    "description": "<Brief>",
    "priority": 1,
    "story_id": "<matched_story_id>",
    "created_by": "triage"
  }'

# Search existing items (for dedup)
curl -sf "$API/api/tasks/?project_id=$PROJECT_ID" | jq '.[].title'

# Reopen a done item (regression)
curl -sf -X POST "$API/api/tasks/<wi_id>/reopen" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Regression found in E2E", "actor": "triage"}'
```

Priority mapping: `0 = CRITICAL, 1 = HIGH, 2 = MEDIUM, 3 = LOW`

## Story Matching

**Every task MUST have a `story_id`.** Before creating a task, match it to the best-fitting story from the loaded list.

Stories are **product-level abstractions** (user value), not technical categories. Examples:
- Pipeline bug, codegen fix, scaffold issue → "Stabilize core pipeline" (user gets working project)
- Internal tooling, skills, dev workflow → "Dev process automation"
- Code splitting, cleanup, refactoring → "Refactoring & code health"
- Security fixes, encryption, audit → "Security hardening"

If no story fits, create the task without `story_id` and list it in the triage report under `### Tasks without story (needs human decision)`.

## Sources

### 1. E2E Reports (`docs/e2e_results/`)

Read all `.md` files in `docs/e2e_results/`. For each report, find the `## Problems Found` section.

For each problem with structured fields:
- **Severity**: critical / major / minor / info
- **Type**: orchestrator / template / meta / infra
- **Backlog**: `#XX` (existing), `new` (create), or `—` (skip)

Only process problems where `Backlog: new` or `Backlog: template`.

- `Backlog: new` → create task via API (based on Type), update report to `Backlog: #XX`.
- `Backlog: template` → route to service-template backlog (see Routing by Type), update to `Backlog: template (triaged)`.

After creating a task, **update the report file** to mark the problem as processed.

If a report has no structured Problems section (old format), scan for issues manually, classify them, and **rewrite the Problems section** in structured format with appropriate `Backlog` values.

### 2. Brainstorms (API + fallback to `docs/brainstorms/`)

**Primary**: Query brainstorms with status=done from the API:
```bash
BRAINSTORMS=$(curl -sf "$API/api/brainstorms/?status=done&project_id=$PROJECT_ID")
```

For each brainstorm, read its `content` field (markdown text).

**Fallback**: Also scan `docs/brainstorms/*.md` for files with `> **Status**: done` that are NOT yet in the DB (legacy brainstorms).

Find the `## Action Items` section in each brainstorm's content. For each item:
- `→ backlog #XX` — already in backlog, skip
- `→ idea: "..."` — add to `docs/ideas.md`
- `→ new task: "..."` — create task via API with `source_brainstorm_id` set:
  ```bash
  curl -sf -X POST "$API/api/tasks/" \
    -H "Content-Type: application/json" \
    -d '{
      "project_id": "'"$PROJECT_ID"'",
      "title": "#'"$NEXT_TAG"' <Title>",
      "type": "feature",
      "description": "<Brief>",
      "priority": 1,
      "created_by": "triage",
      "source_brainstorm_id": "<brainstorm_id>"
    }'
  ```

After processing, mark the brainstorm as triaged:
```bash
curl -sf -X POST "$API/api/brainstorms/<brainstorm_id>/triage" \
  -H "Content-Type: application/json" \
  -d '{"actor": "triage"}'
```

For legacy markdown-only brainstorms, also update the file's Status to `triaged`.

### 3. Audit report (`docs/audit.md`)

If exists, read and extract actionable items not already in backlog.
Search existing tasks via API before creating new ones.

## Routing by Type

| Type | Action |
|------|--------|
| `orchestrator` | Create task via API with `project_id: "$PROJECT_ID"`, `repository_id` for codegen-orchestrator repo |
| `template` | Create task via API with `project_id: "$PROJECT_ID"`, `repository_id` for service-template repo. All tasks go through the API — no more writing to service-template/docs/backlog.md |
| `meta` | Create task via API with `[meta]` prefix in title |
| `infra` | Don't create tasks. Collect and list at the end for human decision. |

Resolve repository IDs at startup:
```bash
REPOS=$(curl -sf "$API/api/repositories/?project_id=$PROJECT_ID")
ORCHESTRATOR_REPO_ID=$(echo "$REPOS" | jq -r '.[] | select(.name == "codegen-orchestrator") | .id')
TEMPLATE_REPO_ID=$(echo "$REPOS" | jq -r '.[] | select(.name == "service-template") | .id')
```

## Regression Detection

Before creating a new task, check done items via API:
```bash
curl -sf "$API/api/tasks/?status=done&project_id=$PROJECT_ID"
```

Search by keywords, affected service, error pattern. If a problem matches a completed item:
1. **Reopen** — `POST /api/tasks/<wi_id>/reopen` with reason
2. **Update** — `PATCH /api/tasks/<wi_id>` to add regression context to description
3. Do NOT create a duplicate task

## Deduplication

Before creating any task, search existing items:
```bash
curl -sf "$API/api/tasks/?project_id=$PROJECT_ID"
```
Search by keywords from the problem description. If a matching item exists, skip (or update its description via PATCH if new info is available).

## Roadmap ↔ Backlog Sync

After processing all sources, synchronize ROADMAP.md with tasks.

### Step 1: Ensure every ROADMAP item has a task

Parse each incomplete (`- [ ]`) item in ROADMAP.md. For each item:
- If it contains `(#XX)` — verify `#XX` exists via `GET /api/tasks/by-tag/XX`. If 404, report as orphan.
- If it has no `(#XX)` — search tasks by keywords for a matching task.
  - **Found** → update the ROADMAP line to include `(#XX)`.
  - **Not found** → report in triage output under `### ROADMAP items without backlog tasks`

### Step 2: Reorder Queue by phases

Compare ROADMAP.md and backlog.md git modification times:
```bash
git log -1 --format="%H %ai" -- docs/ROADMAP.md
git log -1 --format="%H %ai" -- docs/backlog.md
```

Reorder if ROADMAP.md was modified more recently than backlog.md, OR if new tasks were created in this triage run.

How to reorder — update priority on tasks via API:
```bash
curl -sf -X PATCH "$API/api/tasks/<wi_id>" \
  -H "Content-Type: application/json" \
  -d '{"priority": 1}'
```

Priority adjustments:
- Task blocking active story → bump to HIGH if story is critical
- Task deferred to future story → downgrade to LOW
- Add note to description when priority changes

## Sync Docs

After all API changes:
```bash
make sync
```

## Commit (DO NOT push — doc-only commits stay local to avoid wasting CI minutes)

After all processing, commit changed files:

```bash
git add docs/backlog.md docs/e2e_results/ docs/brainstorms/ docs/ideas.md
git commit -m "triage: <N> tasks created, <M> reports processed"
```

Note: template tasks now go through the API (not service-template/docs/backlog.md), so no separate repo commit needed.

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

## Self-Feedback

After completing this skill, if you encountered any of the following — add an entry to `docs/skill-feedback.md`:

- A command or path in this skill was **wrong or outdated**
- A step was **missing context** that you had to figure out yourself
- A step could be **simplified or reordered** for better flow
- The skill **gave ambiguous instructions** that led to a wrong first attempt

Entry format:

```markdown
## [triage] — <today's date>
- **Type**: bug | missing-info | optimization
- **Quote**: "<exact line or section from this skill>"
- **Problem**: <what went wrong or was missing>
- **Suggested fix**: <concrete change to the skill text>
```

Only write feedback that is **specific and actionable**. Skip vague impressions.
