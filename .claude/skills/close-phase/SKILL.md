---
name: close-phase
description: Close the current sprint phase — verify all tasks done, run/write integration tests, advance to next phase.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob, Agent
argument-hint: ""
---

# Close Phase

Gate between phases. Verifies all tasks are done, runs and updates integration tests, then advances the sprint to the next phase.

## Key References
- [docs/TESTING.md](docs/TESTING.md) — test layers, compose files, make targets

## Protocol

### 1. Load state

Read `docs/STATUS.md` — get sprint slug, current phase number.

Read task files for current phase:
```bash
SPRINT_DIR="docs/sprints/<sprint-slug>"
PHASE_NUM=<current phase number>
ls "$SPRINT_DIR/tasks/phase${PHASE_NUM}-"*.md
```

### 2. Verify all tasks done

Check each task file's `## Status:` field. All must be `done`.

If any task is not `done` — STOP: "Phase has incomplete tasks: <list>. Run `/go` to continue."

### 3. Run existing integration tests

```bash
make test-integration 2>&1 | tail -50
```

If tests fail — analyze the failure:
- If caused by changes in this phase → create a fix task in the current phase, set its status to `pending`, STOP: "Integration test failure. Fix task created: <path>. Run `/go`."
- If pre-existing failure unrelated to this phase → note it but proceed

### 4. Assess integration test coverage

Review the code changes made in this phase:
```bash
# Get all commits on current branch since phase started
git log --oneline --name-only HEAD~<commits-in-phase>..HEAD
```

For each changed code path, check:
- Does an integration/service test cover this path?
- Are there new code paths without test coverage?

### 5. Write missing integration tests

If gaps found:
1. Identify which test suite (service test or integration test) is appropriate
2. Write the test following patterns in `docs/TESTING.md`
3. Run the new test to verify it passes
4. Commit the test

If a new integration test **fails** — it found a bug:
1. Create a fix task in the current phase
2. Set status to `pending`
3. STOP: "New integration test found a bug. Fix task created. Run `/go`."

### 6. Update stale tests

Check if any existing tests became stale due to this phase's changes:
- API contract changes → update test assertions
- DTO changes → update test fixtures
- Queue message format changes → update test data

Fix any stale tests, run the full suite, commit.

### 7. Advance phase

Determine the next phase from `sprint.md`:
- If there are more feature phases → advance to next phase
- If this was the last feature phase → sprint enters endgame (audit → e2e → fix → docs)

Update `docs/STATUS.md`:
- Set current phase status to `COMPLETE`
- Set next phase (or "Endgame") as `Current`

### 8. Commit

```bash
git add docs/STATUS.md "$SPRINT_DIR/" tests/
git commit -m "close-phase: phase $PHASE_NUM — <name> COMPLETE"
```

Do NOT push — doc-only commit (unless test files were added).

### 9. Report

> **STOP. Complete skill feedback before reporting.**

**9a. Skill Feedback** — if you hit issues, append to `docs/skill-feedback.md`:

```markdown
## [close-phase] — <today's date>
- **Type**: bug | missing-info | optimization
- **Quote**: "<exact line or section>"
- **Problem**: <what went wrong>
- **Suggested fix**: <concrete change>
```

If nothing went wrong: "Skill feedback: none."

**9b. Print summary:**
```
## Phase N — <Name> COMPLETE

- Tasks completed: M/M
- Integration tests: X existing passed, Y new written
- Stale tests fixed: Z
- Next phase: Phase N+1 — <Name> | Endgame (audit + e2e)
- Run `/go` to continue
```
