---
name: plan-phase
description: Generate task files for the current sprint phase. Reads sprint.md, creates 2-4 task files with descriptions, tests, and acceptance criteria.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: ""
---

# Plan Phase

Generate task files for the current phase of the active sprint.

## Key References
- [docs/TESTING.md](docs/TESTING.md) — test layers, service vs integration tests
- [docs/CONTRACTS.md](docs/CONTRACTS.md) — queue DTOs, shared enums

## Protocol

### 1. Load sprint state

Read `docs/STATUS.md` to get:
- Sprint slug and directory path
- Current phase number and name

Read `docs/sprints/<sprint>/sprint.md` to get:
- Phase description and planned tasks

If no sprint active — STOP: "No active sprint. Run `/new-sprint` first."

### 2. Check phase has no task files yet

```bash
SPRINT_DIR="docs/sprints/<sprint-slug>"
PHASE_NUM=<current phase number>
ls "$SPRINT_DIR/tasks/phase${PHASE_NUM}-"*.md 2>/dev/null
```

If task files already exist — STOP: "Phase already has task files. Run `/go` to continue."

### 3. Research + architectural check

Read the code areas relevant to this phase:
- Files mentioned in the sprint.md phase description
- Related brainstorms if referenced
- Current state of the code that will be modified

Understand dependencies between planned changes.

**Architectural gate**: While reading the code, check for problems that would make the phase work unreliable:
- Files >400 LOC that this phase will modify (should be split first)
- Hardcoded values or raw dicts where this phase needs enums/DTOs
- Missing abstractions that the phase would build on top of
- Copy-paste patterns that the phase would duplicate further

If the foundation is bad — **STOP and report**: "Phase N blocked by architectural issues: <list>. Recommend adding a refactor task as Task 0 in this phase, or creating a separate refactor phase before this one."

Do NOT silently plan tasks on top of bad code. Stopping with a clear report is better than introducing debt.

### 4. Design tasks

Break the phase into 2-4 concrete tasks. Each task should:
- Be completable in one focused session
- Have clear acceptance criteria
- Include test specifications
- Be ordered by dependency (task 1 before task 2 if task 2 depends on it)

Guidelines:
- If a task requires changing `shared/contracts/` or DB schema — mark it `⚠️ needs-approval`
- Separate infrastructure changes (new files, configs) from logic changes
- Last task in the phase should wire everything together if applicable

### 5. Create task files

For each task, create `$SPRINT_DIR/tasks/phase${PHASE_NUM}-task${M}-${slug}.md`:

```markdown
# Phase N Task M: <Title>

## Description
<what needs to change and why — be specific about files and functions>

## Tests First
- <test 1 — what to assert, which test file>
- <test 2>

## Acceptance Criteria
- [ ] <criterion — observable, verifiable>
- [ ] <criterion>

## Status: pending

## Developer Notes
_To be filled during implementation._
```

### 6. Update sprint.md

Update the current phase section in `sprint.md` to reference the created task files:

```markdown
## Phase N: <Name>
- Task 1: <title> → `tasks/phaseN-task1-slug.md`
- Task 2: <title> → `tasks/phaseN-task2-slug.md`
```

### 7. Commit

```bash
git add "$SPRINT_DIR/"
git commit -m "plan: phase $PHASE_NUM — <phase name> ($TASK_COUNT tasks)"
```

Do NOT push — doc-only commit.

### 8. Report

> **STOP. Complete skill feedback before reporting.**

**8a. Skill Feedback** — if you hit issues, append to `docs/skill-feedback.md`:

```markdown
## [plan-phase] — <today's date>
- **Type**: bug | missing-info | optimization
- **Quote**: "<exact line or section>"
- **Problem**: <what went wrong>
- **Suggested fix**: <concrete change>
```

If nothing went wrong: "Skill feedback: none."

**8b. Print summary:**
- Phase: N — <Name>
- Tasks created: M
- Scope: files to touch, tests to write
- Flags: any tasks needing approval
- Next: run `/go` to start implementing
