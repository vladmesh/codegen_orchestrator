---
name: next
description: Pick the next task from backlog and set it as current in STATUS.md. Use when starting a new task or after completing one.
allowed-tools: Read, Edit, Glob
argument-hint: "[#ID]"
---

# Pick Next Task

Select the next task to work on and update STATUS.md.

## Input

- `$ARGUMENTS` — optional backlog item ID (e.g. `#8` or `8`). If omitted, takes the first item from Queue.

## Steps

### 1. Read current state

Read `docs/STATUS.md` and `docs/backlog.md`.

If STATUS.md has a Current Task that is not yet completed — STOP and report:
"There is an active task: #XX. Complete it with `/implement` first, or manually clear STATUS.md."

### 2. Select task

**If argument provided**: find that `#ID` in backlog Queue.
**If no argument**: take the first item in `## Queue` section (top = highest priority).

If the task is not found in Queue — STOP and report.

### 3. Check for plan

Read the task's `Plan` field from backlog:
- If it links to an existing `docs/plans/<file>.md` — plan exists.
- If `—` — no plan.

### 4. Update STATUS.md

Write STATUS.md in this format:

```markdown
# STATUS

## Current Task
- **Backlog**: #<ID> <Title>
- **Plan**: docs/plans/<file>.md (or "—")
- **Step**: 1/<N> — <first step description> (if plan exists, else "—")
- **Done Steps**: (empty)

## Blocked
(нет)

## Last Checkpoint
(keep existing value)
```

Keep the `## Previous work`, `## Quick Links` sections unchanged.

### 5. Report

Print a summary:
- Task selected: #ID — Title
- Priority: HIGH/MEDIUM
- Plan: exists / **needs `/plan` before `/implement`** (if task has no plan and Brief suggests complexity)
- Brief: <from backlog>
