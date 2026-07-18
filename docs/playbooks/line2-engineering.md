# Line 2: Engineering Flow Playbook

Manual test playbook: submit engineering tasks for every valid module combination,
verify that Claude Code, Factory.ai, or OpenAI Codex builds a working project end-to-end.

**Executor**: Human or Claude Code, step by step.
**Not a script** — adapt commands as needed.

---

## Test Matrix

7 test cases covering all valid module combinations (notifications excluded — requires backend and has known service-template issues).

| # | Project Name | Modules | Task Description |
|---|-------------|---------|------------------|
| 1 | `todo_api` | `backend` | REST API for managing TODO items. Endpoints: `GET /todos`, `POST /todos`, `PATCH /todos/{id}`, `DELETE /todos/{id}`. Each TODO has: id, title, description, is_completed, created_at. |
| 2 | `echo_bot` | `tg_bot` | Telegram echo bot. Replies to any text message with the same text reversed. `/start` sends a welcome message explaining what it does. |
| 3 | `landing_page` | `frontend` | Landing page for a product called "TaskFlow". Hero section with tagline, features list (3 items), and a contact form that logs submissions to console. |
| 4 | `weather_bot` | `backend,tg_bot` | Telegram weather bot. `/weather <city>` returns temperature and conditions (mock data, no real API key needed). Backend caches results in PostgreSQL for 30 minutes. `GET /api/weather/{city}` endpoint also available. |
| 5 | `url_shortener` | `backend,frontend` | URL shortener. Backend: `POST /api/shorten` accepts `{url}`, returns short code. `GET /{code}` redirects. `GET /api/stats/{code}` returns click count. Frontend: form to enter URL, displays shortened link, shows click stats. |
| 6 | `bot_landing` | `tg_bot,frontend` | Telegram bot with a landing page. Bot: handles `/start`, `/help`, echoes messages with emoji decoration. Frontend: static page describing the bot features with a "Try it" link to `t.me/botname`. No shared backend. |
| 7 | `expense_tracker` | `backend,tg_bot,frontend` | Personal expense tracker. Backend: CRUD API for expenses and categories, `GET /api/summary` for monthly totals. Bot: `/add <amount> <category> <note>` logs expense, `/summary` shows monthly total. Frontend: dashboard with expense table and category breakdown. |

### Worker Audit

Workers already write `/workspace/REPORT.md` (per INSTRUCTIONS.md) with Issues Encountered
and Suggestions sections. This serves as the audit report — no separate AUDIT_REPORT.md needed.
Worker reports are collected via task events API after each task completes.

---

## Test Levels

### Level A: Code Generation (fastest)

**What**: Engineering worker creates repo, scaffolds, spawns developer worker (or reuses existing worker for story tasks). Developer writes code and pushes. We verify code exists in GitHub. **Do not wait** for CI or deploy.

**Duration**: ~10-20 min per test.

**Verify**:
- GitHub repo created with expected files
- Code committed and pushed (check via GitHub API or `gh repo view`)

**Skip**: CI result, deployment.

### Level B: Engineering + CI

**What**: Full engineering cycle. Wait for task to reach `completed` status (CI gate passed). No deploy.

**Duration**: ~20-40 min per test.

**Verify**:
- Task status = `completed`
- GitHub Actions `ci.yml` passed (green check)
- Code quality (optionally clone and inspect)

**Skip**: Deployment.

### Level C: Full Flow (deploy + verification)

**What**: Engineering + CI + deploy to real server. Verify service is actually running.

**Duration**: ~30-60 min per test.

**Verify**:
- Service deployed and running on server
- Health endpoint responds
- Container logs are clean
- For `backend`: `curl /docs` returns Swagger UI
- For `tg_bot`: container running, no crash loops in logs
- For `frontend`: `curl /` returns HTML

---

## Running a Test

### Prerequisites

> **BLOCKER**: Steps 1-2 must pass. If scaffold doesn't work, Line 2 will silently
> skip it and Claude Code will waste time building from scratch on an empty repo.

1. **Line 1 scaffold test passes**: Run `make test-e2e-scaffold` and verify it succeeds.
   This validates the scaffold phase (copier + make setup + git push) inside a worker container.
2. **Stack is running and healthy**:
   ```bash
   make up
   # Verify ALL services are up (especially api — it can silently fail to start)
   docker compose ps --format "{{.Name}} {{.Status}}" | grep -v "Up"
   # Should output nothing. If api is missing: docker compose up api -d
   curl -s http://localhost:8000/health | jq .
   # Expected: {"status":"ok"}
   ```
3. Engineering worker consuming: check `docker compose logs engineering-worker --tail=5`
4. Worker-manager running: check `docker compose logs worker-manager --tail=5`
5. For Level C: at least one managed server with capacity in DB

### Step 1: Create Project

From host, via API (port 8000):

```bash
PROJECT_ID=$(uuidgen)
PROJECT_NAME="todo_api"  # from test matrix
MODULES="backend"        # from test matrix

# Task description + audit prompt
DESCRIPTION="REST API for managing TODO items. Endpoints: GET /todos, POST /todos, PATCH /todos/{id}, DELETE /todos/{id}. Each TODO has: id, title, description, is_completed, created_at.

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

echo "PROJECT_ID=$PROJECT_ID"
```

### Step 2: Set Secrets (if tg_bot)

Only needed for Level C (real deployment) with `tg_bot` module:

```bash
# Get a test bot token from @BotFather first
curl -s -X PATCH http://localhost:8000/api/projects/$PROJECT_ID \
  -H "Content-Type: application/json" \
  -d '{"config": {"modules": ["backend","tg_bot"], "description": "...", "secrets": {"TELEGRAM_BOT_TOKEN": "YOUR_TOKEN"}}}'
```

> Note: secrets should be Fernet-encrypted in production. For testing, the engineering worker reads them from `config.secrets`.

### Step 3: Trigger Engineering

Two sub-steps: create a task in the API, then publish to the Redis queue.

```bash
# Level A/B: skip deploy
SKIP_DEPLOY=true
# Level C: full flow with deploy
# SKIP_DEPLOY=false

# 3a. Create task via API
TASK_ID="eng-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"

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

# 3b. Publish to engineering queue
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
    print(f'Task {msg.task_id} published to {ENGINEERING_QUEUE}: {mid}')
    await client.close()

asyncio.run(main())
"

echo "TASK_ID=$TASK_ID"
```

Save the `TASK_ID` from output.

### Step 3c: Verify Scaffold Started (do this immediately!)

Within 30 seconds of triggering, check that scaffold is running. If you see
`developer_node_start` **without** a preceding `scaffold_phase_start`, the scaffold
was skipped — **abort immediately** to avoid wasting Claude Code credits.

```bash
sleep 15
docker compose logs worker-manager --tail=20 --since=30s 2>&1 | grep -E "scaffold|copier|creating_worker"
# GOOD: "scaffold_phase_start" or "copier" appears
# BAD:  only "creating_worker" with no scaffold → scaffold was skipped, abort

# If scaffold was skipped, kill the worker:
# docker ps --filter "name=dev-*$PROJECT_NAME*" -q | xargs -r docker rm -f
```

### Step 4: Monitor

**Level A** — poll GitHub repo (don't wait for task completion):

```bash
REPO="project-factory-organization/$PROJECT_NAME"

# Check every 60s if code appeared
while true; do
  FILES=$(gh api repos/$REPO/contents 2>/dev/null | jq -r '.[].name' 2>/dev/null)
  if echo "$FILES" | grep -q "Makefile"; then
    echo "Code pushed! Files:"
    echo "$FILES"
    break
  fi
  echo "Waiting for code push..."
  sleep 60
done
```

**Level B** — wait for task completion:

```bash
TASK_ID="eng-xxxxxxxxxxxx"  # from step 3

while true; do
  STATUS=$(curl -s http://localhost:8000/api/tasks/$TASK_ID | jq -r '.status')
  echo "Task status: $STATUS"
  if [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ]; then
    curl -s http://localhost:8000/api/tasks/$TASK_ID | jq .
    break
  fi
  sleep 30
done
```

**Level C** — wait for engineering + deploy:

```bash
# First wait for engineering task (same as Level B)
# Then check for deploy task:
DEPLOY_TASK=$(curl -s "http://localhost:8000/api/tasks/?type=deploy&project_id=$PROJECT_ID" | jq -r '.[0].id')

while true; do
  STATUS=$(curl -s http://localhost:8000/api/tasks/$DEPLOY_TASK | jq -r '.status')
  echo "Deploy status: $STATUS"
  if [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ]; then
    break
  fi
  sleep 30
done
```

### Step 5: Verify

**Level A** — code exists:

```bash
REPO="project-factory-organization/$PROJECT_NAME"

# Check expected files
gh api repos/$REPO/contents | jq -r '.[].name'

# Expected: Makefile, pyproject.toml, .github, services/, etc.
# For backend: services/backend/
# For tg_bot: services/tg_bot/
# For frontend: services/frontend/

# Check code is not just scaffold (has actual implementation)
gh api repos/$REPO/commits | jq '.[0].commit.message'
```

**Level B** — CI passed:

```bash
# Check GitHub Actions
gh run list -R $REPO --limit 5

# Check specific CI run
gh run view -R $REPO $(gh run list -R $REPO --json databaseId -q '.[0].databaseId')
```

**Level C** — service running:

```bash
# Get deployment info
curl -s "http://localhost:8000/api/service-deployments/?project_id=$PROJECT_ID" | jq .

# From deployment info, get server_handle and port
SERVER_IP="..."  # from server record
PORT="..."       # from deployment record

# Health check
curl -s http://$SERVER_IP:$PORT/health

# For backend: check Swagger docs
curl -s http://$SERVER_IP:$PORT/docs | head -20

# Check container logs (SSH to server)
ssh root@$SERVER_IP "cd /opt/apps/$PROJECT_NAME && docker compose logs --tail=50"

# For tg_bot: verify container is running
ssh root@$SERVER_IP "cd /opt/apps/$PROJECT_NAME && docker compose ps"
```

### Step 6: Collect Worker Reports

Worker reports are stored as task events in the API. Collect them before cleanup:

```bash
# Fetch worker reports from task events
for TASK_ID in $(curl -s "http://localhost:8000/api/tasks/?story_id=$STORY_ID" | \
  python3 -c "import json,sys; [print(t['id']) for t in json.load(sys.stdin)]"); do
  echo "=== Task: $TASK_ID ==="
  curl -s "http://localhost:8000/api/tasks/$TASK_ID/events?event_type=worker_report" | \
    python3 -c "import json,sys; [print(e.get('details',{}).get('report','')) for e in json.load(sys.stdin)]"
done
```

### Step 7: Write E2E Report

After a test completes (pass or fail), write a report to `docs/e2e_results/`.
This step is mandatory — we learn as much from failures as from successes.

#### 7a. Write the investigation report

Create `docs/e2e_results/<project_name>-<date>.md` documenting every problem
encountered during the test. Use existing reports in `docs/e2e_results/` as a
format reference.

For each problem, classify it into one of four types:

| Type | Description | Where to fix |
|------|-------------|--------------|
| **orchestrator** | Bug or issue in this project (codegen_orchestrator) | Fix in this repo |
| **template** | Bug or issue in `service-template` (scaffolding, framework, generated code). The template lives at `/home/vlad/projects/service-template` — read it to confirm root cause. | Fix in service-template repo |
| **meta** | Error in test setup: wrong commands, missing prerequisites, unclear playbook instructions | Fix by updating this playbook |
| **other** | Network failures, hardware issues, transient errors, anything not in the above three | Document and move on |

Report structure:

```markdown
# E2E Investigation: <project_name> — <brief summary>

> **Date**: YYYY-MM-DD
> **Project**: <project_name> (project_id: `...`)
> **Task**: <task_id>
> **Test level**: A / B / C
> **Status**: Passed / Failed
> **Worker audit report**: [audit-<name>-<date>.md](../e2e_results/audit-<name>-<date>.md) (if available)

---

## Timeline
(chronological log of what happened)

## Problems Found

### Problem 1: <title>
- **Type**: orchestrator | template | meta | other
- **Severity**: critical / major / minor
- **Description**: ...
- **Root cause**: ...
- **Suggested fix**: ...

### Problem 2: ...
```

Worker reports (collected in Step 6) contain the developer agent's perspective —
problems it hit while writing code. Reference key findings in the Problems section.

#### 7b. Commit report

```bash
git add docs/e2e_results/${PROJECT_NAME}-$(date +%Y%m%d).md
git commit -m "docs: e2e report for ${PROJECT_NAME}"
```

---

## Cleanup

### Level A / B Cleanup

```bash
REPO="project-factory-organization/$PROJECT_NAME"

# 1. Delete GitHub repo
gh repo delete $REPO --yes

# 2. Kill any running worker containers
docker ps --filter "name=dev-*$PROJECT_NAME*" -q | xargs -r docker rm -f

# 3. Delete project from DB (cascades: tasks + port allocations)
curl -s -X DELETE http://localhost:8000/api/projects/$PROJECT_ID
# Expected: 204 No Content

# 4. Verify cleanup
gh repo view $REPO 2>&1 | grep -q "Not Found" && echo "Repo deleted"
curl -s http://localhost:8000/api/projects/$PROJECT_ID | jq -r '.detail'
# Expected: "Project not found"
```

### Level C Cleanup (everything above + server)

```bash
# 1. Remove from server
ssh root@$SERVER_IP "
  cd /opt/apps/$PROJECT_NAME && docker compose down --remove-orphans --volumes
  rm -rf /opt/apps/$PROJECT_NAME
"

# 2. Delete service deployment records
DEPLOYMENT_IDS=$(curl -s "http://localhost:8000/api/service-deployments/?project_id=$PROJECT_ID" \
  | jq -r '.[].id')
for ID in $DEPLOYMENT_IDS; do
  curl -s -X DELETE http://localhost:8000/api/service-deployments/$ID
done

# 3. Delete GitHub repo
gh repo delete $REPO --yes

# 4. Kill worker containers
docker ps --filter "name=dev-*$PROJECT_NAME*" -q | xargs -r docker rm -f

# 5. Delete project from DB (cascades: tasks + port allocations)
curl -s -X DELETE http://localhost:8000/api/projects/$PROJECT_ID
```

---

## Batch Run Checklist

For running all 7 tests at a given level:

- [ ] #1 `todo_api` (backend)
- [ ] #2 `echo_bot` (tg_bot)
- [ ] #3 `landing_page` (frontend)
- [ ] #4 `weather_bot` (backend,tg_bot)
- [ ] #5 `url_shortener` (backend,frontend)
- [ ] #6 `bot_landing` (tg_bot,frontend)
- [ ] #7 `expense_tracker` (backend,tg_bot,frontend)

**Recommendation**: Run sequentially (one at a time) to avoid resource contention on worker-manager. Each test creates a Docker container that needs CPU/RAM for Claude Code.

---

## Troubleshooting

| Symptom | Likely Cause | Action |
|---------|-------------|--------|
| Task stuck in `running` | Worker container crashed or timed out | Check `docker compose logs worker-manager`, look for container with project name |
| Repo created but empty | Scaffold phase failed | Check `docker compose logs worker-manager` for copier errors |
| CI failed | Generated code has lint/test errors | Clone repo, inspect CI logs via `gh run view -R $REPO` |
| Deploy failed | Server allocation or SSH issues | Check `docker compose logs deploy-worker`, verify server reachability |
| Task `completed` but repo missing | Race condition or wrong org | Check `project.repository_url` via API |

---

## Notes

- **Telegram bot token**: For Levels A/B, not needed (code generation doesn't require real tokens). For Level C with tg_bot, provide a real token from @BotFather before triggering.
- **Parallel runs**: Possible but not recommended. Worker-manager handles one container at a time. Multiple engineering tasks will queue up.
- **Cost**: Each test spawns a paid coding-agent worker session. Budget accordingly.
