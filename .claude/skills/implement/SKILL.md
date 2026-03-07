---
name: implement
description: Implement the current task using TDD. Reads plan from API, creates git branch, updates CHANGELOG on completion. Main development skill.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "[#ID]"
---

# Implement Task

The main development skill. Implements the current task (or a specific one) using TDD workflow.

## Input

- No arguments: continue working on the current in_dev work item
- `#ID` (e.g. `#8`): start that task (calls /start if needed)

## Protocol

### 1. Load context

Find the work item to implement:

**If `#ID` given:**
```bash
WI=$(curl -sf "http://localhost:8000/api/work-items/by-tag/<ID>")
WI_ID=$(echo "$WI" | jq -r '.id')
```

If work item is not in_dev, start it:
```bash
curl -sf -X POST "http://localhost:8000/api/work-items/$WI_ID/start" \
  -H "Content-Type: application/json" \
  -d '{"actor": "claude"}' || true
```

**If no argument:**
```bash
WI=$(curl -sf "http://localhost:8000/api/work-items/?status=in_dev&limit=1" | jq '.[0]')
```

If no in_dev item found — STOP: "No current task. Use `/implement #ID` to start one."

Save `WI_ID` from the response for use in event calls.

### 2. Create git branch

Create and switch to a working branch:
```bash
TAG=$(echo "$WI" | jq -r '.title' | grep -oP '^\#\K\d+')
SLUG=$(echo "$WI" | jq -r '.title' | sed 's/#[0-9]* //' | tr ' ' '-' | tr '[:upper:]' '[:lower:]' | head -c 40)
BRANCH="wi/${TAG}-${SLUG}"
git checkout -b "$BRANCH" 2>/dev/null || git checkout "$BRANCH"
```

If already on a `wi/` branch for this task, stay on it.

### 3. Understand the task

Read the plan from the work item's `plan` field (from API response).
If plan exists, parse the steps to get:
- **Input**: what files/systems to read
- **Output**: what should change
- **Test**: what to test

If no plan exists — read the description and use your judgment.

### 4. TDD cycle (per step)

**Before starting** — emit note event (best-effort):
```bash
curl -sf -X POST "http://localhost:8000/api/work-items/$WI_ID/events" \
  -H "Content-Type: application/json" \
  -d '{"event_type": "note", "details": {"action": "step_start", "step": N, "title": "Step title"}, "actor": "claude"}' || true
```

Follow Red → Green → Refactor:

1. **Red**: Write failing test(s) based on the step's Test spec. Run `make test-unit` to confirm they fail.
2. **Green**: Write minimal code to make tests pass. Run `make test-unit`.
3. **Integration**: If the step is an integration test step from the plan — write the test, but do NOT run locally (CI will run it).
4. **Refactor**: Clean up if needed. Run `make lint` and fix issues.
5. **Commit**: meaningful commit message referencing the backlog item (e.g. `fix(worker): isolate network (#22)`).

**After the commit** — emit note event with commit SHA:
```bash
SHA=$(git rev-parse --short HEAD)
curl -sf -X POST "http://localhost:8000/api/work-items/$WI_ID/events" \
  -H "Content-Type: application/json" \
  -d "{\"event_type\": \"note\", \"details\": {\"action\": \"step_done\", \"step\": N, \"title\": \"Step title\", \"commit_sha\": \"$SHA\"}, \"actor\": \"claude\"}" || true
```

### 5. Push and wait for CI ⛔

**MANDATORY — this is a HARD GATE. Do NOT touch docs (CHANGELOG, backlog) until CI is green.**

After the last step is committed:
1. **Push**: `git push` (or `git push -u origin <branch>` if no upstream)
2. **Poll CI**: `gh run list --branch <branch> --limit 1 --json status` every 60s (up to 15 min)
3. **CI green** → proceed to step 6
4. **CI red** → read logs via `gh run view --log-failed`:
   - **Failure related to current task** — fix, commit, re-push, wait again. Do NOT touch any docs.
   - **Pre-existing failure** (unrelated to current changes) — note it, proceed to step 6.

⛔ **While CI is running or red: NO changes to CHANGELOG.md or backlog generation.** These are completion artifacts.

### 6. Task completion (only after CI green)

⚠️ **Gate**: only enter this step when CI is green (or pre-existing failure documented).

When all steps are done AND CI is green:

**Complete work item via API** (best-effort):
```bash
curl -sf -X POST "http://localhost:8000/api/work-items/$WI_ID/complete" \
  -H "Content-Type: application/json" \
  -d '{"actor": "claude"}' || true
```

**Update `docs/CHANGELOG.md`**:
- Add entry under today's date
- Use correct section: Added / Changed / Fixed / Removed
- Reference backlog item ID

**Regenerate backlog**:
```bash
make backlog
```

**Merge branch** (if on a feature branch):
```bash
git checkout main && git merge --no-ff "$BRANCH"
```

**Commit** doc updates: `docs: complete #<ID> — <title>`

### 7. Report

Print a summary:
- Task: #ID — Title
- Steps completed: N/N
- Tests: X passed, Y added
- Files changed: list
- Next: suggest running `/e2e-run` if core pipeline was touched, or `/implement #ID` to pick next task

## Important

- If you need to change `shared/contracts/` or DB schema that wasn't in the plan — STOP and ask the user.
- If tests are failing and you can't figure out why after 2 attempts — STOP and report the issue.
- Don't skip tests. Every step should have at least one test unless it's pure documentation.
- Run `make lint` before every commit.
