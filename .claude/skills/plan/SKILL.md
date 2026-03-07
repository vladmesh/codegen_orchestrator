---
name: plan
description: Decompose a backlog task into a step-by-step plan. Writes plan to work item via API.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "[#ID]"
---

# Plan Task

Decompose a task into actionable steps. Each step has clear Input, Output, and Test.

## Input

- `#ID` — backlog tag to plan. If omitted, uses current in_dev work item from API.

## Steps

### 1. Load task

Look up the work item:
- If `#ID` given: `curl -sf http://localhost:8000/api/work-items/by-tag/<ID>`
- If no ID: `curl -sf "http://localhost:8000/api/work-items/?status=in_dev&limit=1"` and take first
- If nothing found, try `status=backlog&limit=1`
- If still nothing: STOP — "No current task. Create one via triage or API."

### 2. Load context

Pull all available context for the task:

**Description & plan**: read from the work item response fields.

**Source brainstorm**: check the `source_brainstorm_id` field in the work item response.
If it is not null, fetch the brainstorm:
```bash
BS_ID=$(echo "$WI" | jq -r '.source_brainstorm_id')
if [ "$BS_ID" != "null" ]; then
  curl -sf "http://localhost:8000/api/brainstorms/$BS_ID"
fi
```
Read the brainstorm's `content` field — it contains the full thinking session with context, decisions, and action items.

**Related code**: read all files mentioned in the description, plan, and brainstorm content.

### 3. Research

Before writing the plan:
- Understand the current state of the code
- Identify dependencies between components

### 4. Write plan

Write the plan as a text block in this format:

```
## Context

<Why this task exists. Link to brainstorms if relevant.>
<Current state of the code. What needs to change.>

## Steps

1. [ ] <Step title>
   - **Input**: <files/systems to read or modify>
   - **Output**: <what should exist after this step>
   - **Test**: <unit test description>

2. [ ] <Step title>
   - **Input**: ...
   - **Output**: ...
   - **Test**: ...

...
```

### 5. Guidelines for good steps

- Each step should be completable in one focused session (< 1 hour of agent work)
- Steps should be ordered by dependency (step N should not depend on step N+2)
- Each step includes unit test in the **Test** field
- If several steps stitch components together — add a **separate step** for integration tests
- Last step should be cleanup/documentation if needed
- If a step requires changing `shared/contracts/` or DB schema — mark it explicitly: `⚠️ needs-approval`

### 6. Save plan to API

Write the plan text to the work item:

```bash
WI_ID="<work_item_id from step 1>"
curl -sf -X PATCH "http://localhost:8000/api/work-items/$WI_ID" \
  -H "Content-Type: application/json" \
  -d '{"plan": "<escaped plan text>"}'
```

Use `jq -Rs .` to escape the plan text for JSON if needed.

### 7. Update backlog

Run `make backlog` to regenerate docs/backlog.md from API.

### 8. Commit

```bash
git add docs/backlog.md
git commit -m "plan: #<ID> — <title>"
```

### 9. Report

Print:
- Task: #ID — Title
- Steps: N total
- Estimated scope: files to touch, tests to write
- ⚠️ flags: any steps needing approval, external dependencies, or risks
