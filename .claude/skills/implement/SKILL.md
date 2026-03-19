---
name: implement
description: Implement the current task using TDD. Reads plan from API, creates git branch, updates CHANGELOG on completion. Main development skill.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob, Agent, Skill
argument-hint: "[#ID]"
---

# Implement Task

The main development skill. Implements the current task (or a specific one) using TDD workflow.

## Input

- No arguments: continue working on the current in_dev task, or try to unblock a blocked task, or auto-pick the highest-priority backlog task
- `#ID` (e.g. `#8`): start that task (calls /start if needed)

## Protocol

### 0. Sync git + docs

Ensure local repo is fully synchronized with remote before starting any work:

```bash
# 1. Check for uncommitted changes — commit or stash them
git status --short
# If there are changes: commit doc-only changes, stash anything else

# 2. Pull latest main (fast-forward only to avoid surprise merges)
git checkout main
git pull --ff-only

# 3. If ff-only fails (local diverged from remote), reset to remote:
#    This happens when squash-merged PRs leave orphan local commits.
git reset --hard origin/main
```

After this step, `git status` should show clean working tree on main, up to date with origin.

```bash
make sync
```

### 1. Load context

Find the task to implement:

**If `#ID` given:**
```bash
WI=$(curl -sf "http://localhost:8000/api/tasks/by-tag/<ID>")
WI_ID=$(echo "$WI" | jq -r '.id')
```

If task is not in_dev, start it:
```bash
curl -sf -X POST "http://localhost:8000/api/tasks/$WI_ID/start" \
  -H "Content-Type: application/json" \
  -d '{"actor": "claude"}' || true
```

**If no argument — priority chain:**

**Step A: Check for in_dev task**
```bash
WI=$(curl -sf "http://localhost:8000/api/tasks/?status=in_dev&limit=1" | jq '.[0]')
```

**Step B: If no in_dev — check blocked tasks**
```bash
BLOCKED=$(curl -sf "http://localhost:8000/api/tasks/?status=blocked&limit=10" \
  | jq 'sort_by(.priority)')
```

For each blocked task, check if its blocker is resolved:
```bash
for row in $(echo "$BLOCKED" | jq -c '.[]'); do
  BLOCKER_ID=$(echo "$row" | jq -r '.blocked_by_task_id')
  if [ "$BLOCKER_ID" != "null" ]; then
    BLOCKER_STATUS=$(curl -sf "http://localhost:8000/api/tasks/$BLOCKER_ID" | jq -r '.status')
    if [ "$BLOCKER_STATUS" = "done" ]; then
      # Auto-unblock: transition blocked → in_dev, clear blocked_by_task_id
      TASK_ID=$(echo "$row" | jq -r '.id')
      curl -sf -X POST "http://localhost:8000/api/tasks/$TASK_ID/transition?to_status=in_dev" \
        -H "Content-Type: application/json" \
        -d '{"actor": "claude", "details": {"action": "auto_unblock", "blocker": "'"$BLOCKER_ID"'"}}'
      curl -sf -X PATCH "http://localhost:8000/api/tasks/$TASK_ID" \
        -H "Content-Type: application/json" \
        -d '{"blocked_by_task_id": null}'
      WI=$(curl -sf "http://localhost:8000/api/tasks/$TASK_ID")
      break
    fi
  fi
done
```
Print: "Auto-unblocked: **$TITLE** (blocker $BLOCKER_ID is done)"

If no blocked tasks can be unblocked, proceed to Step C.

**Step C: Auto-pick highest-priority backlog task**
```bash
WI=$(curl -sf "http://localhost:8000/api/tasks/?status=backlog&limit=50" \
  | jq 'sort_by(.priority) | .[0]')
```

If still nothing — STOP: "No tasks in backlog. Create one via /triage or API."

Start the picked task:
```bash
WI_ID=$(echo "$WI" | jq -r '.id')
TITLE=$(echo "$WI" | jq -r '.title')
```
Print: "Auto-picked: **$TITLE** (priority $(echo "$WI" | jq -r '.priority'))"
```bash
curl -sf -X POST "http://localhost:8000/api/tasks/$WI_ID/start" \
  -H "Content-Type: application/json" \
  -d '{"actor": "claude"}'
```

Save `WI_ID` from the response for use in event calls.

**Load event history** (for resume/reopen context):
```bash
EVENTS=$(curl -sf "http://localhost:8000/api/tasks/$WI_ID/events" || echo "[]")
EVENT_COUNT=$(echo "$EVENTS" | jq 'length')
```
If `EVENT_COUNT > 0`, print a summary of previous work:
- Status transitions, completed steps, CI fixes, deviations
- This helps understand where a previous session left off

**Load sibling tasks** (if task came from a brainstorm):
```bash
BS_ID=$(echo "$WI" | jq -r '.source_brainstorm_id')
if [ "$BS_ID" != "null" ]; then
  SIBLINGS=$(curl -sf "http://localhost:8000/api/tasks/?source_brainstorm_id=$BS_ID")
  echo "$SIBLINGS" | jq -r '.[] | select(.id != "'$WI_ID'") | "\(.title) — \(.last_event // "no events")"'
fi
```

### 2. Assess complexity & ensure plan

Check whether the task has a plan:
```bash
HAS_PLAN=$(echo "$WI" | jq -r 'if .plan and .plan != "" then "yes" else "no" end')
```

**If plan exists** — proceed to step 3.

**If no plan** — assess complexity from the description (and brainstorm if available):
- **Simple task** (single file change, clear fix, < 3 files affected): proceed without a plan, use description as guide.
- **Complex task** (multi-file, new feature, schema changes, unclear scope): auto-generate a plan by invoking /plan as a subagent:

```
Use the Agent tool to run: "Run /plan for task $WI_ID. The task: <title>. Description: <description>"
```

After the subagent completes, re-fetch the task to get the saved plan:
```bash
WI=$(curl -sf "http://localhost:8000/api/tasks/$WI_ID")
```

### 3. Create git branch

Create and switch to a working branch:
```bash
TAG=$(echo "$WI" | jq -r '.title' | grep -oP '^\#\K\d+')
SLUG=$(echo "$WI" | jq -r '.title' | sed 's/#[0-9]* //' | tr ' ' '-' | tr '[:upper:]' '[:lower:]' | head -c 40)
BRANCH="wi/${TAG}-${SLUG}"
git checkout -b "$BRANCH" 2>/dev/null || git checkout "$BRANCH"
```

If already on a `wi/` branch for this task, stay on it.

### 4. Understand the task

Read the task's `description` and `plan` fields from the API response.

**Source brainstorm**: check the `source_brainstorm_id` field. If not null, fetch the brainstorm for additional context:
```bash
BS_ID=$(echo "$WI" | jq -r '.source_brainstorm_id')
if [ "$BS_ID" != "null" ]; then
  curl -sf "http://localhost:8000/api/brainstorms/$BS_ID" | jq -r '.content'
fi
```
The brainstorm `content` contains the thinking session with decisions and context that informed this task.

**Plan**: if the `plan` field exists, parse the steps to get:
- **Input**: what files/systems to read
- **Output**: what should change
- **Test**: what to test

If no plan exists (simple task) — use the description (and brainstorm if available) and your judgment.

### 5. TDD cycle (per step)

**Before starting** — emit note event (best-effort):
```bash
curl -sf -X POST "http://localhost:8000/api/tasks/$WI_ID/events" \
  -H "Content-Type: application/json" \
  -d '{"event_type": "note", "details": {"action": "step_start", "step": N, "title": "Step title"}, "actor": "claude"}' || true
```

**If deviating from the plan** (skipping a step, changing approach, adding unplanned work) — emit a deviation event:
```bash
curl -sf -X POST "http://localhost:8000/api/tasks/$WI_ID/events" \
  -H "Content-Type: application/json" \
  -d '{"event_type": "comment", "details": {"action": "plan_deviation", "step": N, "reason": "<why>"}, "actor": "claude"}' || true
```

Follow Red -> Green -> Refactor:

1. **Red**: Write failing test(s) based on the step's Test spec. Run `make test-unit` to confirm they fail.
2. **Green**: Write minimal code to make tests pass. Run `make test-unit`.
3. **Refactor**: Clean up if needed. Run `make lint` and fix issues.
4. **Commit**: meaningful commit message referencing the backlog item (e.g. `fix(worker): isolate network (#22)`).

**After the commit** — emit note event with commit SHA:
```bash
SHA=$(git rev-parse --short HEAD)
curl -sf -X POST "http://localhost:8000/api/tasks/$WI_ID/events" \
  -H "Content-Type: application/json" \
  -d "{\"event_type\": \"note\", \"details\": {\"action\": \"step_done\", \"step\": N, \"title\": \"Step title\", \"commit_sha\": \"$SHA\"}, \"actor\": \"claude\"}" || true
```

### 6. Push + PR + CI

**MANDATORY — this is a HARD GATE. Do NOT touch docs (CHANGELOG, backlog) until CI is green.**

After the last step is committed:

1. **Push branch**:
```bash
git push -u origin "$BRANCH"
```

2. **Create PR** targeting main:
```bash
gh pr create --title "#$TAG — $TITLE" --body "Implements #$TAG"
```

3. **Transition to in_ci**:
```bash
curl -sf -X POST "http://localhost:8000/api/tasks/$WI_ID/transition?to_status=in_ci" \
  -H "Content-Type: application/json" \
  -d '{"actor": "claude"}' || true
```

4. **Poll CI on the PR** — every 60s, up to 15 min:
```bash
gh run list --branch "$BRANCH" --limit 1 --json status,conclusion
```

5. **CI red** — read logs via `gh run view --log-failed`:
   - Fix the issue, commit, push, re-poll. Do NOT touch docs. After fixing, emit a CI-fix event:
     ```bash
     curl -sf -X POST "http://localhost:8000/api/tasks/$WI_ID/events" \
       -H "Content-Type: application/json" \
       -d "{\"event_type\": \"note\", \"details\": {\"action\": \"ci_fix\", \"error\": \"<brief error>\", \"fix\": \"<what was fixed>\"}, \"actor\": \"claude\"}" || true
     ```

6. **CI green** — proceed to step 7.

While CI is running or red: NO changes to CHANGELOG.md or backlog generation.

### 7. Testing (smoke or E2E)

**Gate**: only enter when CI is green (or pre-existing failure documented).

1. **Transition to testing**:
```bash
curl -sf -X POST "http://localhost:8000/api/tasks/$WI_ID/transition?to_status=testing" \
  -H "Content-Type: application/json" \
  -d '{"actor": "claude"}' || true
```

2. **Rebuild and restart services** (MANDATORY before any testing):
```bash
make rebuild
```
This rebuilds images and restarts containers in one step (faster than `make build` + `make up` separately, better cache usage).

3. **Check need_e2e flag**:
```bash
NEED_E2E=$(echo "$WI" | jq -r '.need_e2e')
```

**Simple tasks (need_e2e=false) — Smoke test:**
- `make up` if stack is not running
- Curl API endpoints affected by the change, verify responses
- Check Redis streams if relevant (`docker compose exec redis redis-cli XLEN <stream>`)
- Review structlog output: `docker compose logs --tail=50 <service>` — look for errors
- Confirm no crashes, no unhandled exceptions

**Complex tasks (need_e2e=true) — Full E2E:**
- Run Agent tool with `/e2e-run <test> --no-nuke` in background
- Wait for result

4. **Test red** — fix, commit, push, re-poll CI (step 6.4), re-test.
5. **Test green** — proceed to step 8.

### 8. Merge + Complete

**Gate**: only enter when both CI and testing are green.

1. **Merge PR** (Claude MUST merge — do not leave PR open):
```bash
gh pr merge --squash --delete-branch
```

2. **Switch to main and pull**:
```bash
git checkout main && git pull
```

3. **Complete task via API**:
```bash
curl -sf -X POST "http://localhost:8000/api/tasks/$WI_ID/complete" \
  -H "Content-Type: application/json" \
  -d '{"actor": "claude"}' || true
```

4. **Update `docs/CHANGELOG.md`**:
- Add entry under today's date
- Use correct section: Added / Changed / Fixed / Removed
- Reference backlog item ID

5. **Sync docs**:
```bash
make sync
```

6. **Commit** doc updates on main (DO NOT push — doc-only commits stay local to avoid wasting CI minutes):
```bash
git add docs/CHANGELOG.md docs/backlog.md
git commit -m "docs: complete #<ID> — <title>"
```

### 9. Report

> **STOP. Before writing the summary, complete the Skill Feedback step below. Do NOT skip it.**

**9a. Skill Feedback** — review the session for problems with THIS skill:

Did you hit any of these during this task?
- A command or path in this skill was **wrong or outdated**
- A step was **missing context** that you had to figure out yourself
- A step could be **simplified or reordered** for better flow
- The skill **gave ambiguous instructions** that led to a wrong first attempt

If yes — append an entry to `docs/skill-feedback.md` **right now**, before proceeding:

```markdown
## [implement] — <today's date>
- **Type**: bug | missing-info | optimization
- **Quote**: "<exact line or section from this skill>"
- **Problem**: <what went wrong or was missing>
- **Suggested fix**: <concrete change to the skill text>
```

If nothing went wrong — skip the file write, but you must still explicitly confirm: "Skill feedback: none."

**9b. Emit summary event**:
```bash
curl -sf -X POST "http://localhost:8000/api/tasks/$WI_ID/events" \
  -H "Content-Type: application/json" \
  -d '{"event_type": "comment", "details": {"action": "implementation_summary", "steps_completed": N, "total_steps": N, "deviations": [], "notes": "<brief summary>"}, "actor": "claude"}' || true
```

**9c. Print summary**:
- Task: #ID — Title
- Steps completed: N/N
- Tests: X passed, Y added
- Files changed: list
- Next: suggest running `/e2e-run` if core pipeline was touched, or `/implement` to pick next task

## Important

- If you need to change `shared/contracts/` or DB schema that wasn't in the plan — STOP and ask the user.
- Don't skip tests. Every step should have at least one test unless it's pure documentation.
- Run `make lint` before every commit.

### Failing tests policy

**All tests must pass before pushing.** There is no such thing as "pre-existing failures" — CI blocks merges on red tests, so main is always green. If `make test-unit` shows failures:
- They are caused by your changes (even if indirectly — e.g. you changed a contract/DTO and a test in another service still uses the old shape).
- Find and fix them.
- If a failure reveals a deep architectural problem unrelated to your task that requires serious rework — **STOP immediately**. Do NOT commit, do NOT push. Report the issue to the user with details and wait for guidance.

### Live test policy (service tests & integration tests)

If the plan includes a live/integration test step — **it is mandatory, do not skip it.** "Unit tests already cover it" is not a valid argument — unit tests mock dependencies, live tests verify real wiring. They complement each other but do NOT substitute.

Unit tests = all external services mocked. Can only **supplement** a live test, never **replace** it.

There are two types of live tests. Choose the lightest one that fits:

**Service tests** — single service + its infra deps (db, redis). Fast, **runs automatically in CI**.
- Compose files: `docker/test/service/*.yml` (e.g. `api.yml`, `langgraph.yml`)
- Run: `make test-service SERVICE=<name>`
- Use when: testing one service's behavior against real db/redis, no cross-service calls needed.

**Integration tests** — multiple services wired together. Heavier, **runs in CI only with `run-integration-tests` label** or `workflow_dispatch`.
- Compose files: `docker/test/integration/*.yml` (e.g. `backend.yml`, `template.yml`)
- Each file auto-becomes: `make test-integration-<name>`
- Test code: `tests/integration/<suite>/`
- Use when: testing cross-service flows (e.g. deploy → QA handoff, worker spawning).

**When writing live tests:**
1. Decide: service test or integration test? Prefer service test if only one service + infra is needed.
2. Check existing compose files — can you add to an existing suite?
3. If yes — add a test file to the corresponding directory.
4. If no existing suite fits — create a new compose file. It will auto-register as a make target.
5. **Run locally** after writing: `make test-service SERVICE=<name>` or `make test-integration-<suite>`.
6. For integration tests, also add the `run-integration-tests` label to the PR.
