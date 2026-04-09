---
name: close-sprint
description: Close the current sprint — final gate after endgame. Push all commits, update STATUS.md history, update CHANGELOG.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: ""
---

# Close Sprint

Final sprint gate. Called after all endgame steps (audit, e2e, fix phase, docs update) are complete.

## Protocol

### 1. Verify endgame complete

Read `docs/sprints/<sprint>/sprint.md` endgame section. All must be `done`:
- Audit: done
- E2E: done
- Fix phase: COMPLETE (or skipped if no findings)
- Docs: updated

If any is not done — STOP: "Endgame not complete: <missing>. Run `/go` to continue."

### 2. Verify clean state

```bash
git status --short
```

If uncommitted changes exist — commit them first.

### 3. Fill Results in sprint.md

Add a `## Results` section to `sprint.md`:

```markdown
## Results
- **Completed**: <today's date>
- **Phases**: N completed
- **Tasks**: M total
- **Key changes**: <2-3 bullet points summarizing what was built/fixed>
- **Audit findings**: X fixed in sprint, Y deferred to backlog
- **Decisions made**: <reference Decisions section above>
```

### 4. Update CHANGELOG

Read git log since sprint start date (from sprint.md):
```bash
git log --oneline --since="<sprint start date>"
```

Add a section to `docs/CHANGELOG.md` under today's date:
- Group changes by type: Added / Changed / Fixed / Removed
- Reference sprint number and task IDs where applicable

### 5. Update STATUS.md

Add completed sprint to Sprint History:

```markdown
| NNN | <Goal> | <type> | <start> — <today> | <phase count> |
```

Reset current sprint section:

```markdown
## Current Sprint

No active sprint. Run `/new-sprint` to begin.

## Phase Progress

_No sprint active._
```

### 6. Update ROADMAP.md

For each task completed in this sprint:
- Find it in ROADMAP.md
- Mark as `[x]` if present
- If all tasks in a story are done, note the story as COMPLETE

### 7. Move deferred items to backlog

Read `## Deferred` section from `sprint.md`. For each item:
- Add to `docs/backlog.md` under `## Queue` with appropriate priority
- Mark as processed in sprint.md

### 8. Push

```bash
git add docs/STATUS.md docs/CHANGELOG.md docs/ROADMAP.md docs/backlog.md "docs/sprints/<sprint>/"
git commit -m "sprint: close <NNN>-<slug>"
git push
```

This is the ONE push point per sprint. All doc-only commits from the sprint get pushed together.

### 9. Report

> **STOP. Complete skill feedback before reporting.**

**8a. Skill Feedback** — if you hit issues, append to `docs/skill-feedback.md`:

```markdown
## [close-sprint] — <today's date>
- **Type**: bug | missing-info | optimization
- **Quote**: "<exact line or section>"
- **Problem**: <what went wrong>
- **Suggested fix**: <concrete change>
```

If nothing went wrong: "Skill feedback: none."

**8b. Print summary:**
```
## Sprint NNN — <Goal> COMPLETE

- Type: feature | tech
- Duration: <start> — <today>
- Phases completed: N
- Tasks completed: M
- CHANGELOG updated: Y entries
- ROADMAP items marked done: Z
- Deferred to backlog: K items
- Next: run `/go` to start next sprint
```
