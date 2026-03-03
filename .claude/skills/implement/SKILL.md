---
name: implement
description: Implement the current task from STATUS.md using TDD. Updates CHANGELOG, backlog, STATUS on completion. Main development skill.
disable-model-invocation: true
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "[#ID | step]"
---

# Implement Task

The main development skill. Implements the current task (or a specific one) using TDD workflow.

## Input

- No arguments: continue working on the current task from `docs/STATUS.md`
- `#ID` (e.g. `#8`): switch to that task (hotfix flow — updates STATUS.md first)
- `step` (e.g. `3`): jump to a specific step of the current plan

## Protocol

### 1. Load context

Read `docs/STATUS.md` to get:
- Current backlog item ID and title
- Plan file path (if exists)
- Current step number

If `#ID` argument provided and differs from current — update STATUS.md to point to the new task first (like `/next` would).

If no current task in STATUS.md — STOP: "No current task. Use `/next` to pick one."

### 2. Understand the task

Read the plan file (if exists) to get the current step's:
- **Input**: what files/systems to read
- **Output**: what should change
- **Test**: what to test

If no plan exists — read the task Brief from `docs/backlog.md` and use your judgment.

### 3. TDD cycle (per step)

Follow Red → Green → Refactor:

1. **Red**: Write failing test(s) based on the step's Test spec. Run `make test-unit` to confirm they fail.
2. **Green**: Write minimal code to make tests pass. Run `make test-unit`.
3. **Integration**: If the step is an integration test step from the plan — write the test, but do NOT run locally (CI will run it).
4. **Refactor**: Clean up if needed. Run `make lint` and fix issues.
5. **Commit**: meaningful commit message referencing the backlog item (e.g. `fix(worker): isolate network (#22)`).

### 4. Update step progress

After each completed step, update `docs/STATUS.md`:
- Move current step to Done Steps
- Advance Step to next
- If plan exists, mark step as `[x]` in the plan file

### 5. CI gate

After the last step is committed and pushed:
- Wait for CI to finish: poll `gh run list --branch <branch> --limit 1 --json status` every 60s (up to 15 min)
- If CI passes — proceed to completion
- If CI fails — read the logs via `gh run view --log-failed`:
  - **Failure related to current task** — fix, re-push, wait again. Do NOT mark task as done.
  - **Pre-existing failure** (unrelated to current changes) — add to `docs/backlog.md` as `Priority: critical`, proceed to completion. Note the pre-existing failure in the commit message.

### 6. Task completion

When all steps are done AND CI is green:

**Update `docs/CHANGELOG.md`**:
- Add entry under today's date
- Use correct section: Added / Changed / Fixed / Removed
- Reference backlog item ID

**Update `docs/backlog.md`**:
- Move task from Queue to Done section (keep last 10 in Done, remove oldest if needed)
- Format: `- #<ID> <Title> — <today's date>`

**Update `docs/STATUS.md`**:
- Clear Current Task section (set all fields to "—")
- The Done Steps remain as a record until `/next` overwrites

**Annotate plan** (if exists):
- Do NOT delete `docs/plans/<task>.md`
- Add a `## Deviations` section at the end with notes on where implementation diverged from the plan
- Mark all steps `[x]` and add inline comments if a step was done differently

**Commit** all doc updates together: `docs: complete #<ID> — <title>`

### 7. Report

Print a summary:
- Task: #ID — Title
- Steps completed: N/N
- Tests: X passed, Y added
- Files changed: list
- Next: suggest running `/e2e-run` if core pipeline was touched, or `/next` to pick next task

## Important

- If you need to change `shared/contracts/` or DB schema that wasn't in the plan — STOP and ask the user.
- If tests are failing and you can't figure out why after 2 attempts — STOP and report the issue.
- Don't skip tests. Every step should have at least one test unless it's pure documentation.
- Run `make lint` before every commit.
