---
name: next
description: Pick the next task from backlog and set it as current in STATUS.md. Use when starting a new task or after completing one.
allowed-tools: Bash, Read, Edit, Glob
argument-hint: "[#ID]"
---

# Pick Next Task

Select the next task to work on and update STATUS.md. Uses Work Items API.

## Input

- `$ARGUMENTS` — optional backlog item ID (e.g. `#8` or `8`). If omitted, takes the first item by priority from backlog.

## Steps

### 1. Read current state

Read `docs/STATUS.md`.

If STATUS.md has a Current Task that is not yet completed — STOP and report:
"There is an active task: #XX. Complete it with `/implement` first, or manually clear STATUS.md."

### 2. Select task via API

**If argument provided** (e.g. `#53` or `53`):
```bash
curl -s http://localhost:8000/api/work-items/by-tag/53
```
This returns the work item whose title starts with `#53 `.

**If no argument** — take the first backlog item by priority:
```bash
curl -s "http://localhost:8000/api/work-items/?status=backlog&limit=1"
```
This returns a JSON array with at most 1 item, sorted by priority (lowest = first).

If the API returns 404 or empty array — STOP and report: "No backlog items available."

If the API is unreachable — STOP and report: "API not available. Run `make up` first."

Save the work item JSON for later steps. Extract: `id`, `title`, `description`, `priority`.

### 3. Extract tag ID

From the work item title (e.g. `"#53 Compose runner fix"`), extract the numeric tag: `53`.
This is used for STATUS.md and commit message.

### 4. Check for plan

Read `docs/backlog.md`, find the task by `### #<tag>` header, check its `Plan` field:
- If it links to an existing `docs/plans/<file>.md` — plan exists. Read the plan to get step count and first step title.
- If `—` — no plan.

### 5. Start work item

Transition the work item to `in_dev` via API:
```bash
curl -s -X POST "http://localhost:8000/api/work-items/<id>/start" \
  -H "Content-Type: application/json" \
  -d '{"actor": "claude"}'
```

This handles backlog → todo → in_dev transitions automatically.

### 6. Update STATUS.md

Write STATUS.md Current Task section:

```markdown
## Current Task
- **Backlog**: #<tag> <Title>
- **WorkItem**: <work_item_id> (e.g. wi-3372a29b — from the API response `id` field)
- **Plan**: docs/plans/<file>.md (or "—")
- **Step**: 1/<N> — <first step description> (if plan exists, else "—")
- **Done Steps**: (empty)
```

Keep the `## Blocked`, `## Last Checkpoint`, `## Previous work`, `## Quick Links` sections unchanged.

### 7. Commit

```bash
git add docs/STATUS.md
git commit -m "next: #<tag> — <title>"
```

### 8. Report

Print a summary:
- Task selected: #tag — Title
- Priority: value from API
- Status: in_dev (started via API)
- Plan: exists / **needs `/plan` before `/implement`**
- Brief: description from API response
