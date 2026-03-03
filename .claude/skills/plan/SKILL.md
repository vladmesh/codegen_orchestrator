---
name: plan
description: Decompose a backlog task into a step-by-step plan with Input/Output/Test for each step. Creates docs/plans/<task>.md and updates STATUS.md.
disable-model-invocation: true
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "[#ID]"
---

# Plan Task

Decompose a task into actionable steps. Each step has clear Input, Output, and Test.

## Input

- `#ID` — backlog item to plan. If omitted, uses current task from `docs/STATUS.md`.

## Steps

### 1. Load task

Read the task from `docs/backlog.md` (find by ID in Queue).
Read related brainstorms if referenced in Brief.
Read related code to understand the scope.

### 2. Research

Before writing the plan:
- Read all files mentioned in the task's Brief
- Understand the current state of the code
- Identify dependencies between components
- Check if there are related brainstorms in `docs/brainstorms/`

### 3. Write plan

Create `docs/plans/<task-slug>.md` using this format:

```markdown
# Plan: <Title> (#<ID>)

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
- Each step включает unit test в поле **Test**
- Если несколько шагов сшивают компоненты — добавь **отдельный шаг** на написание integration test
- Last step should be cleanup/documentation if needed
- If a step requires changing `shared/contracts/` or DB schema — mark it explicitly: `⚠️ needs-approval`

### 5. Update STATUS.md

Update the Current Task section:
- Set `Plan` to the new plan file path
- Set `Step` to `1/<total> — <first step title>`
- Clear `Done Steps`

### 6. Update backlog

Set the task's Plan field in `docs/backlog.md` to the new plan file path.

### 7. Commit

```bash
git add docs/plans/<task>.md docs/STATUS.md docs/backlog.md
git commit -m "plan: #<ID> — <title>"
```

### 8. Report

Print:
- Task: #ID — Title
- Steps: N total
- Estimated scope: files to touch, tests to write
- ⚠️ flags: any steps needing approval, external dependencies, or risks
