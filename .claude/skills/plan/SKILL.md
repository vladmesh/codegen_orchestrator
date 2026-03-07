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

Read related brainstorms if referenced in description.
Read related code to understand the scope.

### 2. Research

Before writing the plan:
- Read all files mentioned in the task's description
- Understand the current state of the code
- Identify dependencies between components
- Check if there are related brainstorms in `docs/brainstorms/`

### 3. Write plan

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

### 4. Guidelines for good steps

- Each step should be completable in one focused session (< 1 hour of agent work)
- Steps should be ordered by dependency (step N should not depend on step N+2)
- Each step includes unit test in the **Test** field
- If several steps stitch components together — add a **separate step** for integration tests
- Last step should be cleanup/documentation if needed
- If a step requires changing `shared/contracts/` or DB schema — mark it explicitly: `⚠️ needs-approval`

### 5. Save plan to API

Write the plan text to the work item:

```bash
WI_ID="<work_item_id from step 1>"
curl -sf -X PATCH "http://localhost:8000/api/work-items/$WI_ID" \
  -H "Content-Type: application/json" \
  -d '{"plan": "<escaped plan text>"}'
```

Use `jq -Rs .` to escape the plan text for JSON if needed.

### 6. Update backlog

Run `make backlog` to regenerate docs/backlog.md from API.

### 7. Commit

```bash
git add docs/backlog.md
git commit -m "plan: #<ID> — <title>"
```

### 8. Report

Print:
- Task: #ID — Title
- Steps: N total
- Estimated scope: files to touch, tests to write
- ⚠️ flags: any steps needing approval, external dependencies, or risks
