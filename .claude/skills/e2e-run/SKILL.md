---
name: e2e-run
description: Run Line 2 E2E test — submit engineering task, wait for completion, verify, write report. Use when user wants to test the engineering pipeline end-to-end.
disable-model-invocation: true
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "<test> <level> [--no-cleanup]"
---

# E2E Engineering Test Runner

Run one or more Line 2 E2E tests end-to-end: create project, trigger engineering,
monitor progress, verify results, collect audit report, write investigation report, cleanup.

## Arguments

- `$0` — test selector (REQUIRED):
  - Project name: `todo_api`, `echo_bot`, `landing_page`, etc.
  - Test number: `1`, `2`, `3`, etc.
  - Comma-separated: `1,3,5` or `todo_api,echo_bot`
  - `all` — run all 7 tests sequentially
- `$1` — test level (default: `A`):
  - `A` — Code generation only (fastest, ~10-20 min). Verify code in GitHub.
  - `B` — Engineering + CI (~20-40 min). Wait for task completion + CI pass.
  - `C` — Full flow + deploy (~30-60 min). Verify service running on server.
- `--no-cleanup` — skip cleanup after test (keep repo, containers, DB records)

## Test Matrix

| # | Name | Modules | Description |
|---|------|---------|-------------|
| 1 | `todo_api` | `backend` | REST API for TODO items. `GET/POST/PATCH/DELETE /todos`. Fields: id, title, description, is_completed, created_at. |
| 2 | `echo_bot` | `tg_bot` | Telegram echo bot. Reverses text. `/start` sends welcome. |
| 3 | `landing_page` | `frontend` | "TaskFlow" landing page. Hero, 3 features, contact form (logs to console). |
| 4 | `weather_bot` | `backend,tg_bot` | `/weather <city>` returns mock data. Backend caches in PG 30min. `GET /api/weather/{city}` also available. |
| 5 | `url_shortener` | `backend,frontend` | `POST /api/shorten` → short code. `GET /{code}` redirects. Frontend: form + stats. |
| 6 | `bot_landing` | `tg_bot,frontend` | Bot echoes with emoji. Frontend: static page describing bot. No shared backend. |
| 7 | `expense_tracker` | `backend,tg_bot,frontend` | CRUD expenses + categories. Bot: `/add`, `/summary`. Frontend: dashboard + breakdown. |

## Audit Prompt (appended to every task description)

```
## Audit Instructions

In addition to completing the task above, you are performing an audit of the
framework and development environment.

Throughout your work, keep a file called AUDIT_REPORT.md in the repo root.
Log everything you encounter:
- Problems, errors, or unexpected behavior
- Missing features or tools in the framework
- Anything that didn't work as expected or required workarounds
- Suggestions for improving the template, framework, or workspace setup
- Ideas for making the development flow smoother

Be specific: include exact error messages, file paths, and what you expected
vs what happened. This report is as valuable as the code itself.
```

## Execution Flow

For each selected test case, execute these steps. If running multiple tests,
run them **sequentially** (one at a time — worker-manager handles one container at a time).

### Step 0: Quick health check

Before the first test, verify the stack is healthy:

```bash
curl -sf http://localhost:8000/health | jq .
docker compose ps --format "{{.Name}} {{.Status}}" | grep -v "Up"
```

If API is not healthy, STOP and tell the user to fix the stack first.

### Step 1: Create project

```bash
PROJECT_ID=$(uuidgen)
PROJECT_NAME="<from matrix>"
MODULES="<from matrix>"

DESCRIPTION="<task description from matrix>

## Audit Instructions

In addition to completing the task above, you are performing an audit of the framework and development environment. Throughout your work, keep a file called AUDIT_REPORT.md in the repo root. Log everything you encounter: problems, errors, unexpected behavior, missing features or tools in the framework, anything that didn't work as expected or required workarounds, suggestions for improving the template, framework, or workspace setup, ideas for making the development flow smoother. Be specific: include exact error messages, file paths, and what you expected vs what happened."

curl -s -X POST http://localhost:8000/api/projects/ \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
    --arg id "$PROJECT_ID" \
    --arg name "$PROJECT_NAME" \
    --arg modules "$MODULES" \
    --arg desc "$DESCRIPTION" \
    '{
      id: $id,
      name: $name,
      status: "draft",
      config: {
        modules: ($modules | split(",")),
        description: $desc
      }
    }')" | jq .
```

Save `PROJECT_ID` — you'll need it for all subsequent steps.

### Step 2: Trigger engineering

Set `SKIP_DEPLOY` based on level:
- Level A or B: `SKIP_DEPLOY=true`
- Level C: `SKIP_DEPLOY=false`

```bash
TASK_ID="eng-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"

# Create task in API
curl -s -X POST http://localhost:8000/api/tasks/ \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
    --arg id "$TASK_ID" \
    --arg pid "$PROJECT_ID" \
    '{
      id: $id,
      type: "engineering",
      project_id: $pid,
      task_metadata: {triggered_by: "cli", action: "create"},
      callback_stream: "agent:events:manual-test"
    }')" | jq .

# Publish to engineering queue
docker compose exec -T langgraph python -c "
import asyncio
from shared.contracts.queues.engineering import EngineeringMessage
from shared.redis.client import RedisStreamClient
from shared.queues import ENGINEERING_QUEUE

async def main():
    client = RedisStreamClient()
    await client.connect()
    msg = EngineeringMessage(
        task_id='$TASK_ID',
        project_id='$PROJECT_ID',
        user_id='manual-test',
        action='create',
        skip_deploy=$SKIP_DEPLOY,
        callback_stream='agent:events:manual-test',
    )
    mid = await client.publish_message(ENGINEERING_QUEUE, msg)
    print(f'Published: task={msg.task_id} mid={mid}')
    await client.close()

asyncio.run(main())
"
```

Save `TASK_ID`.

### Step 3: Verify scaffold started (CRITICAL — do immediately!)

Wait 15 seconds, then check worker-manager logs:

```bash
sleep 15
docker compose logs worker-manager --tail=30 --since=60s 2>&1 | grep -E "scaffold|copier|creating_worker"
```

**GOOD**: `scaffold_phase_start` or `copier` appears in output.
**BAD**: only `creating_worker` with no scaffold → scaffold was SKIPPED.

If scaffold was skipped: **ABORT this test immediately**. Kill the worker container,
document "scaffold skipped" in report, and move to the next test or stop.

```bash
# Abort: kill worker
docker ps --filter "name=dev-" --format "{{.Names}}" | grep "$PROJECT_NAME" | xargs -r docker rm -f
```

### Step 4: Monitor

Poll based on level. Print status updates every check.

**Level A** — poll GitHub for code (don't wait for task completion):

```bash
REPO="project-factory-organization/$PROJECT_NAME"
# Poll every 60s, timeout after 30 minutes
for i in $(seq 1 30); do
  FILES=$(gh api repos/$REPO/contents 2>/dev/null | jq -r '.[].name' 2>/dev/null)
  if echo "$FILES" | grep -q "Makefile"; then
    echo "Code pushed at attempt $i"
    break
  fi
  echo "[$i/30] Waiting for code push..."
  sleep 60
done
```

**Level B** — poll task status:

```bash
# Poll every 30s, timeout after 60 minutes
for i in $(seq 1 120); do
  STATUS=$(curl -s http://localhost:8000/api/tasks/$TASK_ID | jq -r '.status')
  echo "[$i/120] Task status: $STATUS"
  if [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ]; then
    break
  fi
  sleep 30
done
```

**Level C** — poll engineering task, then deploy task:

First wait for engineering (same as Level B), then:

```bash
# Find deploy task
DEPLOY_TASK=$(curl -s "http://localhost:8000/api/tasks/?type=deploy&project_id=$PROJECT_ID" | jq -r '.[0].id')

# Poll deploy every 30s, timeout after 30 minutes
for i in $(seq 1 60); do
  STATUS=$(curl -s http://localhost:8000/api/tasks/$DEPLOY_TASK | jq -r '.status')
  echo "[$i/60] Deploy status: $STATUS"
  if [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ]; then
    break
  fi
  sleep 30
done
```

### Step 5: Verify

**Level A** — code exists in GitHub:

```bash
REPO="project-factory-organization/$PROJECT_NAME"
gh api repos/$REPO/contents | jq -r '.[].name'
# Check for expected files: Makefile, pyproject.toml, services/
# Verify code is not just scaffold — check last commit message
gh api repos/$REPO/commits | jq '.[0].commit.message'
```

**Level B** — CI passed:

```bash
gh run list -R $REPO --limit 5
# Check latest CI run status
gh run view -R $REPO $(gh run list -R $REPO --json databaseId -q '.[0].databaseId') 2>/dev/null
```

**Level C** — service running on server:

```bash
# Get deployment info
curl -s "http://localhost:8000/api/service-deployments/?project_id=$PROJECT_ID" | jq .
# Extract server IP and port, then:
# curl health endpoint, check container logs, etc.
```

### Step 6: Collect worker audit report

Try to fetch `AUDIT_REPORT.md` that the developer worker commits to the repo.

```bash
REPO="project-factory-organization/$PROJECT_NAME"
DATE=$(date +%Y%m%d)
mkdir -p docs/e2e_results

# Try GitHub first
AUDIT=$(gh api repos/$REPO/contents/AUDIT_REPORT.md 2>/dev/null | jq -r '.content' 2>/dev/null | base64 -d 2>/dev/null)

# Fallback: workspace on disk
if [ -z "$AUDIT" ]; then
  AUDIT=$(cat /tmp/codegen/workspaces/project-${PROJECT_ID}/workspace/AUDIT_REPORT.md 2>/dev/null)
fi

# Save if found
if [ -n "$AUDIT" ]; then
  echo "$AUDIT" > "docs/e2e_results/${PROJECT_NAME}-${DATE}-worker.md"
  echo "Worker audit report saved"
else
  echo "No worker audit report found"
fi
```

### Step 7: Write E2E report

Write your own report to `docs/e2e_results/<project_name>-<date>.md`.
If worker audit was collected in Step 6, link to it from the report header.

**File naming pattern** (two files per test, linked by name prefix):
- `docs/e2e_results/<project_name>-<date>.md` — your report (this step)
- `docs/e2e_results/<project_name>-<date>-worker.md` — worker's audit (Step 6, if available)

Use existing reports in `docs/e2e_results/` as format reference if any exist.

Classify each problem by type:

| Type | What it means | Where to look |
|------|---------------|---------------|
| **orchestrator** | Bug in this project (codegen_orchestrator) | This repo |
| **template** | Bug in `service-template` (scaffolding, framework, generated code) | `/home/vlad/projects/service-template` — read source there to confirm root cause |
| **meta** | Error in this skill's instructions (wrong commands, missing steps, bad assumptions) | Read `.claude/skills/e2e-run/SKILL.md` to find the incorrect instruction. Document what's wrong but do NOT edit the skill file. |
| **other** | Network failures, transient errors, hardware issues | Document and move on |

Report structure:

```markdown
# E2E Report: <project_name> — <brief summary>

> **Date**: YYYY-MM-DD
> **Project**: <project_name> (project_id: `...`)
> **Task**: <task_id>
> **Test level**: A / B / C
> **Status**: Passed / Failed
> **Worker audit**: [<project_name>-<date>-worker.md](./<project_name>-<date>-worker.md) (if available)

---

## Timeline
(chronological log of what happened — key timestamps and events)

## Problems Found

### Problem 1: <title>
- **Type**: orchestrator | template | meta | other
- **Severity**: critical / major / minor
- **Description**: ...
- **Root cause**: ...
- **Suggested fix**: ...
```

### Step 8: Cleanup (skip if --no-cleanup)

```bash
# 1. Kill worker containers
docker ps --filter "name=dev-" --format "{{.Names}}" | grep "$PROJECT_NAME" | xargs -r docker rm -f

# 2. Delete GitHub repo
gh repo delete project-factory-organization/$PROJECT_NAME --yes
```

**Level C only** — also clean server deployment:

```bash
# 3. Get deployment info (server IP, port, deployment IDs)
DEPLOYMENTS=$(curl -s "http://localhost:8000/api/service-deployments/?project_id=$PROJECT_ID")
SERVER_IP=$(echo "$DEPLOYMENTS" | jq -r '.[0].server_ip // empty')

# 4. Remove app from server
if [ -n "$SERVER_IP" ]; then
  ssh root@$SERVER_IP "
    cd /opt/apps/$PROJECT_NAME && docker compose down --remove-orphans --volumes
    rm -rf /opt/apps/$PROJECT_NAME
  "
fi

# 5. Delete deployment records
echo "$DEPLOYMENTS" | jq -r '.[].id' | while read ID; do
  curl -s -X DELETE "http://localhost:8000/api/service-deployments/$ID"
done
```

**All levels** — delete project from DB last (cascades tasks + port allocations):

```bash
# 6. Delete project from DB
curl -s -X DELETE http://localhost:8000/api/projects/$PROJECT_ID
```

## Final Summary

After all tests complete, print a summary table:

```
## E2E Test Results

| # | Project | Level | Status | Duration | Problems | Audit |
|---|---------|-------|--------|----------|----------|-------|
| 1 | todo_api | B | PASS | 25min | 0 | Yes |
| 2 | echo_bot | B | FAIL | 18min | 2 | No |
...

Total: X passed, Y failed out of Z tests
```

## Error Handling

- If a step fails, **document the failure** in the report and continue to the next step.
- Do NOT stop the entire run on a single failure — collect as much data as possible.
- If scaffold is skipped, abort that specific test but continue to the next test in batch.
- If the API is unreachable, STOP everything — the stack is down.
- Always attempt cleanup even if the test failed (unless --no-cleanup).
