---
name: plan
description: Decompose a backlog task into a step-by-step plan. Writes plan to docs/plans/.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "[#ID]"
---

# Plan Task

Decompose a task into actionable steps. Each step has clear Input, Output, and Test.

## Key References
- [docs/DEV_PIPELINE.md](docs/DEV_PIPELINE.md) — task lifecycle
- [docs/CONTRACTS.md](docs/CONTRACTS.md) — shared DTOs and enums

## Input

- `#ID` — backlog tag to plan. If omitted, picks the first task from `docs/backlog.md` Queue.

## Steps

### 1. Load task

Find the task in `docs/backlog.md`:
- If `#ID` given: search for `### #<ID>` section
- If no ID: take the first `### #` entry under `## Queue`
- If nothing found: STOP — "No tasks in backlog."

Extract: tag, title, priority, brief/description.

### 2. Load context

**Source brainstorm**: search `docs/brainstorms/` for files related to this task. Check if the task's Brief mentions a brainstorm file or topic. Read any matching brainstorm for full context.

**Related tasks**: scan `docs/backlog.md` for tasks with similar keywords or from the same brainstorm.

**Related code**: read all files mentioned in the description and brainstorm content.

### 3. Research

Before writing the plan:
- Understand the current state of the code
- Identify dependencies between components

### 4. Write plan

Write the plan to `docs/plans/<tag>-<slug>.md`:

```markdown
# #<tag> <Title>

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

### 6. Update backlog

Update the task's entry in `docs/backlog.md` to note it has a plan:
```
- **Plan**: [docs/plans/<tag>-<slug>.md](docs/plans/<tag>-<slug>.md)
```

### 7. Commit (DO NOT push — doc-only commits stay local to avoid wasting CI minutes)

```bash
git add docs/plans/<tag>-<slug>.md docs/backlog.md
git commit -m "plan: #<ID> — <title>"
```

### 8. Report

> **STOP. Before writing the summary, complete the Skill Feedback step below. Do NOT skip it.**

**8a. Skill Feedback** — review the session for problems with THIS skill:

Did you hit any of these during this task?
- A command or path in this skill was **wrong or outdated**
- A step was **missing context** that you had to figure out yourself
- A step could be **simplified or reordered** for better flow
- The skill **gave ambiguous instructions** that led to a wrong first attempt

If yes — append an entry to `docs/skill-feedback.md` **right now**, before proceeding:

```markdown
## [plan] — <today's date>
- **Type**: bug | missing-info | optimization
- **Quote**: "<exact line or section from this skill>"
- **Problem**: <what went wrong or was missing>
- **Suggested fix**: <concrete change to the skill text>
```

If nothing went wrong — skip the file write, but you must still explicitly confirm: "Skill feedback: none."

**8b. Print summary**:
- Task: #ID — Title
- Steps: N total
- Estimated scope: files to touch, tests to write
- Flags: any steps needing approval, external dependencies, or risks
