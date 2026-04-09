---
name: new-sprint
description: Create a new sprint — checks tech sprint cadence, proposes scope from VISION/ROADMAP/backlog, creates sprint directory and files.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "[feature|tech] [goal description]"
---

# New Sprint

Create a new sprint with phases and goal. Checks whether a tech sprint is due.

## Key References
- [docs/VISION.md](docs/VISION.md) — product direction and architectural invariants. **Read first** to align sprint scope with where the product is heading.
- [docs/ROADMAP.md](docs/ROADMAP.md) — story-level milestones and progress
- [docs/backlog.md](docs/backlog.md) — deferred pool (tech debt, ideas)

## Input

- `feature <goal>` — create a feature sprint with given goal
- `tech` — force a tech sprint from backlog
- No args — auto-decide based on cadence and backlog size

## Protocol

### 1. Check preconditions

Read `docs/STATUS.md`. If a sprint is active and not COMPLETE — STOP: "Current sprint is not finished. Run `/close-sprint` first or `/go` to continue."

### 2. Determine sprint type

**If explicit arg** — use it.

**If auto-decide:**

1. Count sprints in Sprint History table of `docs/STATUS.md`
2. Count how many sprints since last `tech` sprint
3. If ≥4 sprints since last tech sprint → recommend tech sprint
4. Read `docs/backlog.md` — if >30 items in Queue → recommend tech sprint regardless
5. Otherwise → ask user: "Feature sprint or tech sprint? Backlog has N items, last tech sprint was N sprints ago."

### 3. Determine sprint number and slug

```bash
# Get next sprint number from Sprint History
LAST_NUM=$(grep -oP '^\| \K\d+' docs/STATUS.md | sort -n | tail -1)
NEXT_NUM=$((LAST_NUM + 1))
# If no history yet, start with 001
```

Slug: kebab-case from goal (e.g., `001-tech-backlog-cleanup`).

### 4. Determine scope (for tech sprints)

Read `docs/backlog.md`. Group tasks by priority and category:

1. **Security** issues → highest priority
2. **Code smells** / contract violations → medium
3. **Infra** improvements → medium
4. **Nice-to-have** / ideas → low

Select 5-10 tasks that form a coherent theme. If tasks are diverse, pick a unifying angle (e.g., "code quality cleanup", "security hardening").

### 5. Determine scope (for feature sprints)

**Read `docs/VISION.md` first.** The product direction section tells you where the product is heading — what the user wants to build. Use this to:
- Propose a sprint goal that advances the product vision
- Prioritize capabilities the user explicitly described as important
- Avoid work that contradicts the "Что НЕ делаем" section

Then read `docs/ROADMAP.md` — find the next incomplete story that aligns with VISION.

- Goal comes from user input, VISION.md direction, or next incomplete ROADMAP story
- Break goal into 2-4 phases (logical chunks of work)
- Each phase = a coherent set of changes that can be tested together
- When proposing scope to the user, explain WHY this sprint matters in terms of VISION.md

### 6. Design phases

Each sprint has 2-4 feature/tech phases. The endgame phases (audit, e2e, fix, docs) are NOT listed as sprint phases — they are implicit in the sprint lifecycle.

Phase design guidelines:
- Phase 0 = lowest-risk, foundational changes
- Each subsequent phase builds on the previous
- Each phase should be completable in 1-3 tasks
- Phase scope should be testable independently

### 7. Create sprint directory and files

```bash
SPRINT_DIR="docs/sprints/${NEXT_NUM}-${SLUG}"
mkdir -p "$SPRINT_DIR/tasks"
```

Write `$SPRINT_DIR/sprint.md`:

```markdown
# Sprint NNN: <Title>

> **Goal**: <one sentence>
> **Type**: feature | tech
> **Started**: <today's date>

## Phase 0: <Name>
- <task description — files created by /plan-phase>

## Phase 1: <Name>
- <task description — files created by /plan-phase>

## Decisions
_None yet._

## Deferred
_None yet._

## Endgame
- Audit: pending
- E2E: pending
- Fix phase: pending
- Docs: pending
```

### 8. Update STATUS.md

```markdown
## Current Sprint
- **Sprint**: NNN-<slug>
- **Goal**: <goal>
- **Type**: feature | tech
- **Started**: <today's date>
- **Current Phase**: Phase 0 — <name>

## Phase Progress
| Phase | Name | Status |
|-------|------|--------|
| 0 | <name> | Current |
| 1 | <name> | Pending |
```

Keep the Sprint History table intact — it gets updated by `/close-sprint`.

### 9. Commit

```bash
git add docs/STATUS.md "$SPRINT_DIR/sprint.md"
git commit -m "sprint: start $NEXT_NUM-$SLUG"
```

Do NOT push — doc-only commit.

### 10. Report

```
## New Sprint Created

- **Sprint**: NNN-<slug>
- **Type**: feature | tech
- **Goal**: <goal>
- **Phases**: N phases planned
- **Next**: run `/go` to start Phase 0 (will invoke /plan-phase)
```

## Self-Feedback

If you encountered issues during this skill, append to `docs/skill-feedback.md`:

```markdown
## [new-sprint] — <today's date>
- **Type**: bug | missing-info | optimization
- **Quote**: "<exact line or section from this skill>"
- **Problem**: <what went wrong or was missing>
- **Suggested fix**: <concrete change to the skill text>
```
