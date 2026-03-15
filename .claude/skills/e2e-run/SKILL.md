---
name: e2e-run
description: Run Line 2 E2E test — submit engineering task, wait for completion, verify, write report. Use when user wants to test the engineering pipeline end-to-end.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "<test> [--feature] [--no-cleanup] [--no-nuke]"
---

# E2E Engineering Test Runner

Run one or more E2E tests end-to-end: create project via the full pipeline
(scaffold → architect → engineering → deploy), monitor progress, verify results,
collect reports, cleanup.

## Arguments

- `$0` — test selector (REQUIRED):
  - Project name: `todo_api`, `weather_bot`
  - Test number: `1`, `2`
  - Comma-separated: `1,2` or `todo_api,weather_bot`
  - `all` — run both tests sequentially
- `--feature` — after create+deploy succeeds, trigger a feature-add on the same project
- `--no-cleanup` — skip cleanup (keep repo, containers, DB records)
- `--no-nuke` — skip `make nuke` in Step 0 (assume stack is already clean)

## Test Matrix

| # | Name | Modules | Description |
|---|------|---------|-------------|
| 1 | `todo_api` | `backend` | REST API for TODO items. `GET/POST/PATCH/DELETE /todos`. Fields: id, title, description, is_completed, created_at. |
| 2 | `weather_bot` | `backend,tg_bot` | `/weather <city>` returns mock data. Backend caches in PG 30min. `GET /api/weather/{city}` also available. |

## Feature Add Matrix (for --feature mode)

| # | Name | Feature Description |
|---|------|---------------------|
| 1 | `todo_api` | Add `GET /todos/stats` endpoint that returns `{"total": N, "completed": N, "pending": N}` counting all todos. |
| 2 | `weather_bot` | Add `/forecast <city>` command that returns mock 3-day forecast (today, tomorrow, day after). Backend endpoint `GET /api/forecast/{city}`. |

## Audit Prompt (appended to every task description)

```
## Audit Instructions

In addition to completing the task above, you are performing an audit of the
framework and development environment. Throughout your work, keep a file called
AUDIT_REPORT.md in the repo root. Log everything you encounter: problems, errors,
unexpected behavior, missing features or tools in the framework, anything that
didn't work as expected or required workarounds, suggestions for improving the
template, framework, or workspace setup, ideas for making the development flow
smoother. Be specific: include exact error messages, file paths, and what you
expected vs what happened.
```

## Architecture Quick Reference

Understanding the pipeline flow helps you know where to look when things stall:

```
Project (DRAFT) + Repository + Story (CREATED)
  ↓
scaffold_trigger (scheduler, 30s cycle)
  → publishes ScaffoldMessage to scaffold:queue
  ↓
Scaffolder container
  → copier template + make setup + git push
  → project: DRAFT → ACTIVE, workspace_ready=true
  ↓
architect:queue → Architect container (separate from langgraph!)
  → waits for scaffold (polls 10s, max 5min)
  → story: CREATED → IN_PROGRESS
  → LLM decomposes story into Tasks + appends CI check task
  ↓
Task Dispatcher (scheduler, 30s cycle)
  → finds TODO tasks with no blockers
  → creates Run record, publishes EngineeringMessage to engineering:queue
  → task: TODO → IN_DEV
  ↓
Engineering Worker (langgraph container, separate entrypoint)
  → spawns worker container via worker-manager
  → worker runs Claude CLI agent
  → agent commits, pushes, waits for CI
  → task: IN_DEV → DONE (or FAILED)
  ↓
All tasks DONE → Dispatcher detects
  → story: IN_PROGRESS → DEPLOYING
  → publishes DeployMessage to deploy:queue
  ↓
Deploy Worker (langgraph container, separate entrypoint)
  → configures GitHub secrets
  → triggers deploy.yml workflow
  → runs smoke test
  → story: DEPLOYING → COMPLETED
```

**Key containers** (each is separate in docker-compose):
- `langgraph` — PO agent
- `architect` — story decomposition (NOT inside scheduler!)
- `scheduler` — scaffold trigger, task dispatcher, story completion
- `engineering-worker` — engineering consumer
- `deploy-worker` — deploy consumer
- `worker-manager` — spawns worker containers
- `scaffolder` — project scaffolding

## E2E Secrets (for tg_bot tests)

Tests with `tg_bot` module need a `TELEGRAM_BOT_TOKEN` for deploy.
Secrets are read from `.claude/e2e-secrets.env` (gitignored).

**Injection**: After creating the project, if the test includes `tg_bot`,
inject secrets into `project.config.secrets`:

```bash
TG_TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' .claude/e2e-secrets.env 2>/dev/null | cut -d= -f2-)

if [ -z "$TG_TOKEN" ]; then
  echo "ERROR: TELEGRAM_BOT_TOKEN not found in .claude/e2e-secrets.env"
  # STOP this test — cannot deploy without token
fi

docker compose exec -T -e "TG_TOKEN=$TG_TOKEN" -e "PROJECT_ID=$PROJECT_ID" api python -c "
import os, asyncio
import httpx
from shared.crypto import encrypt_dict

async def main():
    pid = os.environ['PROJECT_ID']
    token = os.environ['TG_TOKEN']
    async with httpx.AsyncClient(base_url='http://localhost:8000') as api:
        r = await api.get(f'/api/projects/{pid}')
        project = r.json()
        config = project['config']
        existing = config.get('secrets', {})
        new_secrets = encrypt_dict({'TELEGRAM_BOT_TOKEN': token})
        existing.update(new_secrets)
        config['secrets'] = existing
        await api.patch(f'/api/projects/{pid}', json={'config': config})
        print(f'Injected TELEGRAM_BOT_TOKEN into project {pid}')

asyncio.run(main())
"
```

## GitHub Access

**IMPORTANT**: The local `gh` CLI does NOT have access to `project-factory-organization`.
Always use `GitHubAppClient` via docker compose exec:

```bash
docker compose exec -T api python -c "
import asyncio
from shared.clients.github import GitHubAppClient

async def main():
    gh = GitHubAppClient()
    # gh.list_repo_files(owner, repo, path='', ref='main') -> list[str]
    # gh.get_file_contents(owner, repo, path, ref='main') -> str | None
    # gh.get_latest_workflow_run(owner, repo, workflow_file, branch, created_after=None) -> dict
    # gh.delete_repo(owner, repo) -> None
    # gh.create_repo(org, name, description, private) -> dict
    # gh.get_org_token(org) -> str
    result = await gh.list_repo_files('project-factory-organization', 'REPO_NAME')
    print(result)

asyncio.run(main())
"
```

## Server Access

SSH keys are stored in the DB (encrypted). Use the helper script:

```bash
bash infra/scripts/ssh-to-server.sh $SERVER_IP "hostname"
```

**Server filesystem layout**: `/opt/services/<PROJECT_NAME>/`

```bash
# Docker compose on server — always use both compose files:
COMPOSE="docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml"
$COMPOSE ps -a
$COMPOSE logs backend --tail=50
```

## Execution Flow

Run tests **sequentially** (one at a time).

**Repo naming**: GitHub repos use **hyphens**, not underscores.
Define `REPO_SLUG=$(echo "$PROJECT_NAME" | tr '_' '-')` early.
Use `$REPO_SLUG` for GitHub API, `$PROJECT_NAME` for API/DB/server paths.

### Step 0: Health check + pre-flight cleanup

**Skip `make nuke` if `--no-nuke` is set.**

```bash
make nuke
```

Then verify the stack:

```bash
curl -sf http://localhost:8000/health | jq .
docker compose ps --format "{{.Name}} {{.Status}}" | grep -v "Up"
```

If API is not healthy, STOP.

**Worker image staleness check**:

```bash
CURRENT_HASH=$(find shared packages/worker-wrapper packages/orchestrator-cli \
  services/worker-manager/images -type f \
  -not -path '*/__pycache__/*' -not -name '*.pyc' \
  | LC_ALL=C sort | xargs sha256sum 2>/dev/null | sha256sum | cut -c1-16)

STORED_HASH=$(docker inspect worker-base-common:latest \
  --format '{{index .Config.Labels "org.codegen.worker_source_hash"}}' 2>/dev/null || echo "none")

if [ "$CURRENT_HASH" != "$STORED_HASH" ]; then
  echo "Worker images stale — rebuilding..."
  make rebuild-worker-images
else
  echo "Worker images up to date"
fi
```

**Pre-flight cleanup** (run for every test):

```bash
ORG="project-factory-organization"
REPO_SLUG=$(echo "$PROJECT_NAME" | tr '_' '-')

# 1. Delete leftover GitHub repo
REPO_EXISTS=$(docker compose exec -T api python -c "
import asyncio
from shared.clients.github import GitHubAppClient
async def main():
    gh = GitHubAppClient()
    try:
        await gh.list_repo_files('$ORG', '$REPO_SLUG')
        print('EXISTS')
    except Exception:
        print('CLEAN')
asyncio.run(main())
" 2>/dev/null | tail -1)

if [ "$REPO_EXISTS" = "EXISTS" ]; then
  echo "WARNING: Leftover repo — deleting"
  docker compose exec -T api python -c "
import asyncio
from shared.clients.github import GitHubAppClient
async def main():
    gh = GitHubAppClient()
    await gh.delete_repo('$ORG', '$REPO_SLUG')
    print('Deleted')
asyncio.run(main())
"
fi

# 2. Kill leftover worker containers
docker ps --filter "label=com.codegen.type=worker" --format "{{.Names}}" | xargs -r docker rm -f

# 3. Clean stale deployments on servers
for SERVER_IP in $(curl -s "http://localhost:8000/api/servers/?is_managed=true" | jq -r '.[].public_ip'); do
  HAS_DIR=$(bash infra/scripts/ssh-to-server.sh $SERVER_IP \
    "[ -d /opt/services/$PROJECT_NAME ] && echo EXISTS || echo CLEAN" 2>/dev/null || echo "SSH_FAIL")
  if [ "$HAS_DIR" = "EXISTS" ]; then
    echo "WARNING: Stale deployment on $SERVER_IP — cleaning"
    bash infra/scripts/ssh-to-server.sh $SERVER_IP "
      cd /opt/services/$PROJECT_NAME/infra 2>/dev/null && \
        docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml down -v --remove-orphans 2>/dev/null || true
      rm -rf /opt/services/$PROJECT_NAME
    "
  fi
done
```

### Step 0.5: Queue Health Check

**Before starting, check queues for stale messages.** Stale messages from previous
runs can clog the architect for hours — each triggers a full LLM call or 5-min scaffold timeout.

Cross-check the Debug API and raw Redis:

```bash
# Debug API (preferred)
curl -s http://localhost:8000/debug/queues | python3 -m json.tool

# Architect queue messages
curl -s "http://localhost:8000/debug/queues/architect:queue/messages?count=50" | python3 -c "
import json, sys
data = json.load(sys.stdin)
print(f'Total messages: {data[\"total\"]}')
for m in data['messages']:
    story_id = m['data'].get('story_id', '?')
    print(f\"  {m['id']}  story={story_id}  ts={m['timestamp']}\")
"

# Raw Redis cross-check
docker compose exec -T api python3 -c "
import asyncio, redis.asyncio as redis
async def check():
    r = redis.from_url('redis://redis:6379')
    for q in ['architect:queue', 'engineering:queue', 'deploy:queue', 'scaffold:queue']:
        try:
            length = await r.xlen(q)
            print(f'{q}: {length} messages')
        except:
            print(f'{q}: does not exist')
    await r.aclose()
asyncio.run(check())
"
```

**If queue length > 5**: Clean stale messages via API:

```bash
curl -s "http://localhost:8000/debug/queues/architect:queue/messages?count=200" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for m in data['messages']:
    story_id = m['data'].get('story_id', '?')
    print(f\"{m['id']}  story={story_id}\")
print(f'Total: {data[\"total\"]}')
"

# Delete stale messages (repeat per ID)
curl -X DELETE "http://localhost:8000/debug/queues/architect:queue/messages/<message_id>"

# Ack stuck pending messages
curl -X POST "http://localhost:8000/debug/queues/architect:queue/architect-consumers/ack/<message_id>"
```

### Step 1: Create via PO agent

**1a. Upsert test user**:

```bash
curl -s -X POST http://localhost:8000/api/users/upsert \
  -H "Content-Type: application/json" \
  -d '{
    "telegram_id": 999000001,
    "username": "e2e_test_user",
    "first_name": "E2E",
    "last_name": "Test",
    "is_admin": false
  }' | jq .
```

**1b. Send to PO via `po:input`**.

Compose a natural-language message with project name, modules, description, and audit instructions.

```bash
REQUEST_ID=$(python3 -c 'import uuid; print(uuid.uuid4())')
E2E_USER_ID="999000001"

docker compose exec -T \
  -e "REQUEST_ID=$REQUEST_ID" \
  -e "E2E_USER_ID=$E2E_USER_ID" \
  -e "MESSAGE_TEXT=$MESSAGE_TEXT" \
  api python -c "
import os, asyncio
from shared.contracts.queues.po import POUserMessage, to_flat_fields
from shared.redis.client import RedisStreamClient
from shared.queues import PO_INPUT_QUEUE

async def main():
    client = RedisStreamClient()
    await client.connect()
    msg = POUserMessage(
        text=os.environ['MESSAGE_TEXT'],
        user_id=os.environ['E2E_USER_ID'],
        request_id=os.environ['REQUEST_ID'],
    )
    mid = await client.publish_flat(PO_INPUT_QUEUE, to_flat_fields(msg))
    print(f'Published to po:input: mid={mid}')
    await client.close()

asyncio.run(main())
"
```

**1c. Wait for PO response** (timeout 120s):

```bash
docker compose exec -T -e "REQUEST_ID=$REQUEST_ID" api python -c "
import os, asyncio

async def main():
    import redis.asyncio as redis
    r = redis.from_url('redis://redis:6379')
    request_id = os.environ['REQUEST_ID']
    stream = f'po:response:{request_id}'
    for attempt in range(24):
        result = await r.xread({stream: '0'}, block=5000, count=1)
        if result:
            for stream_name, messages in result:
                for mid, fields in messages:
                    text = fields.get(b'text', b'').decode()
                    error = fields.get(b'error', b'').decode()
                    if error:
                        print(f'PO ERROR: {error}')
                    else:
                        print(f'PO RESPONSE: {text}')
                    await r.delete(stream)
                    await r.aclose()
                    return
    print('TIMEOUT: PO did not respond within 120s')
    await r.aclose()

asyncio.run(main())
"
```

**1d. Extract IDs**:

```bash
PROJECT_ID=$(curl -s "http://localhost:8000/api/projects/" \
  | jq -r --arg name "$PROJECT_NAME" '.[] | select(.name == $name) | .id' | head -1)

STORY_ID=$(curl -s "http://localhost:8000/api/stories/?sort=-created_at" \
  | jq -r --arg pid "$PROJECT_ID" '.[] | select(.project_id == $pid) | .id' | head -1)

echo "PROJECT_ID=$PROJECT_ID STORY_ID=$STORY_ID"
```

If either is empty, send a follow-up to PO. If PO fails after 2 attempts,
note "PO failed to create project" in report and stop the test.

**1e. If test includes `tg_bot`** — inject secrets (see "E2E Secrets").

### Step 2: Monitor Scaffold

Scaffold is triggered automatically by `scaffold_trigger` (runs every 30s in scheduler).
It checks for DRAFT projects with stories and repositories.

```bash
# Poll project status — scaffold transitions DRAFT → ACTIVE
for i in $(seq 1 20); do
  STATUS=$(curl -s "http://localhost:8000/api/projects/$PROJECT_ID" | jq -r '.status')
  WORKSPACE=$(curl -s "http://localhost:8000/api/projects/$PROJECT_ID" | jq -r '.config.workspace_ready // false')
  echo "[$i/20] Project: status=$STATUS workspace_ready=$WORKSPACE"
  if [ "$STATUS" = "active" ] && [ "$WORKSPACE" = "true" ]; then
    echo "Scaffold complete"
    break
  fi
  sleep 15
done
```

**If stuck after 5 minutes**: Check scaffolder and scheduler logs:

```bash
docker compose logs scaffolder --tail=30 --since=5m 2>/dev/null
docker compose logs scheduler --tail=30 --since=5m 2>/dev/null | grep -i scaffold
```

**Light intervention**: If scaffold_trigger hasn't fired, check the scaffold:queue
and inflight key. If there's a stale `scaffold:inflight:{project_id}` Redis key, clear it:

```bash
docker compose exec -T api python3 -c "
import asyncio, redis.asyncio as redis
async def main():
    r = redis.from_url('redis://redis:6379')
    key = 'scaffold:inflight:$PROJECT_ID'
    val = await r.get(key)
    if val:
        await r.delete(key)
        print(f'Cleared stale inflight key: {key}')
    else:
        print('No inflight key found')
    await r.aclose()
asyncio.run(main())
"
```

### Step 3: Monitor Architect

The architect runs in its **own container** (not scheduler!).

```bash
# Poll for tasks created by architect
for i in $(seq 1 30); do
  TASKS=$(curl -s "http://localhost:8000/api/tasks/?story_id=$STORY_ID&sort=created_at")
  COUNT=$(echo "$TASKS" | jq 'length')
  echo "[$i/30] Tasks: $COUNT"
  if [ "$COUNT" -gt "0" ]; then
    echo "$TASKS" | jq -r '.[] | "\(.id)  \(.status)  \(.title)"'
    break
  fi
  sleep 10
done
```

**If no tasks after 5 minutes**: Check architect logs:

```bash
docker compose logs architect --tail=50 --since=5m 2>/dev/null | grep -v "HTTP Request" | tail -20
```

**Known architect issues**:

- **`blocked_by_task_id="None"` string bug**: Architect LLM returns Python `"None"` instead
  of `null`. Causes 500 FK violation. Check API logs:
  ```bash
  docker compose logs api --tail=200 --since=5m 2>/dev/null | grep -A5 "500\|ForeignKey"
  ```
  If this happens: note in report as a finding. Do NOT create tasks manually — just document it.

- **Scaffold timeout**: If project is still DRAFT, architect waits up to 5 min. Check if
  scaffold actually completed (see Step 2).

**Verify**: Task chain should have sensible descriptions, correct blocking order,
and a CI check task at the end.

### Step 4: Monitor Engineering

Track each task through its lifecycle. This is the longest phase.

```bash
# Poll task statuses every 30-60s
curl -s "http://localhost:8000/api/tasks/?story_id=$STORY_ID&sort=created_at" | python3 -c "
import json, sys
tasks = json.load(sys.stdin)
for t in tasks:
    blocked = f' (blocked by {t.get(\"blocked_by_task_id\",\"\")})' if t.get('blocked_by_task_id') else ''
    print(f\"{t['id']}  {t['status']:25s}  {t['title']}{blocked}\")
"
```

### What to check at each status:

**`todo` → `in_dev`**: Task dispatcher picks up unblocked TODO tasks every 30s.
- If stuck > 2 min and not blocked: check scheduler logs
  ```bash
  docker compose logs scheduler --tail=30 --since=5m 2>/dev/null | grep -v "HTTP Request" | tail -15
  ```

**`in_dev`**: Worker is running.
- Check via Worker-Manager Introspection API:
  ```bash
  curl -s http://localhost:8000/wm-api/workers/ | python3 -c "
  import json, sys
  for w in json.load(sys.stdin):
      print(f\"{w['container_name']}  status={w['status']}  task={w.get('task_id','?')}\")
  "
  # Worker logs
  curl -s "http://localhost:8000/wm-api/workers/$WORKER_ID/logs?tail=30"
  # Worker progress
  curl -s "http://localhost:8000/wm-api/workers/$WORKER_ID/files/PROGRESS.md"
  ```
- Cross-check with Docker:
  ```bash
  docker ps --filter "label=com.codegen.type=worker" --format "{{.Names}}\t{{.Status}}"
  ```
- If API and Docker disagree — note as a finding.

**`in_ci`**: Code pushed, CI running.
```bash
docker compose exec -T api python -c "
import asyncio
from shared.clients.github import GitHubAppClient
async def main():
    gh = GitHubAppClient()
    run = await gh.get_latest_workflow_run('project-factory-organization', '$REPO_SLUG', 'ci.yml', 'main')
    if run:
        print(f\"CI: {run['status']} / {run.get('conclusion','pending')} — {run['html_url']}\")
    else:
        print('No CI run found')
asyncio.run(main())
"
```

**`done`**: Task completed. Collect worker report:
```bash
curl -s "http://localhost:8000/api/tasks/$TASK_ID/events?event_type=worker_report" | python3 -c "
import json, sys
for e in json.load(sys.stdin):
    report = e.get('details', {}).get('report', '')
    if report: print(report)
    else: print('(no worker report)')
"
```

**`failed`**: Check why. Read failure_metadata and events:
```bash
curl -s http://localhost:8000/api/tasks/$TASK_ID | python3 -c "
import json, sys
t = json.load(sys.stdin)
print('Status:', t['status'])
print('Failure metadata:', json.dumps(t.get('failure_metadata'), indent=2))
"
```

**Light intervention on failure**: If the failure looks retriable (timeout, transient error),
retry the task:
```bash
curl -X POST "http://localhost:8000/api/tasks/$TASK_ID/transition?to_status=backlog"
curl -X POST "http://localhost:8000/api/tasks/$TASK_ID/transition?to_status=todo"
```
Document the retry in the timeline. If it fails again, record and move on.

### Polling loop

Poll every 30-60 seconds. Timeout after 30 minutes per task.

Keep a timeline log:
```
HH:MM  task-xxx  todo → in_dev (worker started)
HH:MM  task-xxx  in_dev → in_ci (commit pushed)
HH:MM  task-xxx  CI passed → done
```

### Step 5: Monitor Deploy

When all tasks are done, the dispatcher transitions the story to `deploying` and publishes
a DeployMessage. This happens automatically — just watch.

**IMPORTANT**: The dispatcher's `complete_stories` only checks stories in `in_progress` status.
If the story is stuck in `created` after all tasks are done (shouldn't happen in normal flow
but can with race conditions):

```bash
# Light intervention: transition story to in_progress
curl -s -X POST "http://localhost:8000/api/stories/$STORY_ID/start" \
  -H "Content-Type: application/json" \
  -d '{"actor": "e2e-test"}'
```

### Story API: Action-based endpoints

Stories use action-based endpoints, NOT generic PATCH:
```
POST /api/stories/{id}/start     → created → in_progress
POST /api/stories/{id}/deploy    → in_progress → deploying
POST /api/stories/{id}/complete  → in_progress/deploying → completed
POST /api/stories/{id}/fail      → any → failed
POST /api/stories/{id}/reopen    → completed/failed → in_progress
```

### Monitoring deploy

```bash
# Watch story status
curl -s http://localhost:8000/api/stories/$STORY_ID | python3 -c "
import json, sys
s = json.load(sys.stdin)
print(f\"Story: {s['status']}\")
"

# Deploy worker logs
docker compose logs deploy-worker --tail=50 --since=5m 2>/dev/null | grep -v "HTTP Request" | tail -20
```

Poll story status every 30s, timeout after 30 minutes. Wait for `completed` or `failed`.

### Step 6: Verify Deployment

**6a. CI status**:

```bash
docker compose exec -T api python -c "
import asyncio
from shared.clients.github import GitHubAppClient
async def main():
    gh = GitHubAppClient()
    run = await gh.get_latest_workflow_run('project-factory-organization', '$REPO_SLUG', 'ci.yml', 'main')
    if run:
        print(f\"CI: {run['status']} / {run.get('conclusion')}\")
        print(f\"URL: {run['html_url']}\")
asyncio.run(main())
"
```

**6b. Find server and verify**:

```bash
# Find deployed URL from service-deployments
curl -s "http://localhost:8000/api/service-deployments/?project_id=$PROJECT_ID" | python3 -c "
import json, sys
deps = json.load(sys.stdin)
for d in deps:
    print(f\"URL: http://{d.get('server_ip')}:{d.get('port')}\")
    print(f\"Server: {d.get('server_ip')}\")
"
```

**If deploy succeeded**:

```bash
bash infra/scripts/ssh-to-server.sh $SERVER_IP "
  cd /opt/services/$PROJECT_NAME/infra
  COMPOSE='docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml'
  echo '=== Container status ==='
  \$COMPOSE ps -a
  echo '=== Restart counts ==='
  for cid in \$(\$COMPOSE ps -q 2>/dev/null); do
    restarts=\$(docker inspect --format '{{.RestartCount}}' \"\$cid\")
    name=\$(docker inspect --format '{{.Name}}' \"\$cid\")
    echo \"\$name: restarts=\$restarts\"
  done
"

# Health check
curl -sf "http://$SERVER_IP:$DEPLOY_PORT/health" | jq . || echo "Health endpoint not responding"
```

**If deploy failed** — collect crash diagnostics:

```bash
bash infra/scripts/ssh-to-server.sh $SERVER_IP "
  PROJECT_DIR=/opt/services/$PROJECT_NAME
  if [ ! -d \"\$PROJECT_DIR\" ]; then
    echo 'No deployment directory found'
    exit 0
  fi

  echo '=== .env contents ==='
  cat \$PROJECT_DIR/.env 2>/dev/null || echo 'NO .env FILE'

  cd \$PROJECT_DIR/infra
  COMPOSE='docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml'

  echo '=== Container status ==='
  \$COMPOSE ps -a 2>/dev/null || echo 'No containers'

  echo '=== Backend logs ==='
  \$COMPOSE logs backend --tail=50 2>/dev/null || echo 'No backend logs'

  echo '=== DB logs ==='
  \$COMPOSE logs db --tail=20 2>/dev/null || echo 'No db logs'
"
```

### Step 7: Collect Reports

**7a. Worker reports from task events**:

```bash
for TASK_ID in $(curl -s "http://localhost:8000/api/tasks/?story_id=$STORY_ID" | python3 -c "
import json, sys
for t in json.load(sys.stdin):
    print(t['id'])
"); do
  echo "=== Task: $TASK_ID ==="
  curl -s "http://localhost:8000/api/tasks/$TASK_ID/events?event_type=worker_report" | python3 -c "
import json, sys
events = json.load(sys.stdin)
for e in events:
    report = e.get('details', {}).get('report', '')
    if report: print(report)
    else: print('(no worker report)')
"
  echo
done
```

**7b. Audit report from GitHub repo**:

```bash
ORG="project-factory-organization"
mkdir -p docs/e2e_results/worker_reports
DATE=$(date +%Y%m%d)
WORKER_REPORT="docs/e2e_results/worker_reports/${PROJECT_NAME}-${DATE}-worker.md"

docker compose exec -T api python -c "
import asyncio
from shared.clients.github import GitHubAppClient
async def main():
    gh = GitHubAppClient()
    content = await gh.get_file_contents('$ORG', '$REPO_SLUG', 'AUDIT_REPORT.md')
    if content: print(content)
    else: print('NOT_FOUND')
asyncio.run(main())
" 2>/dev/null > /tmp/audit_report.txt

if grep -q "NOT_FOUND" /tmp/audit_report.txt; then
  echo "No worker audit report found"
else
  cp /tmp/audit_report.txt "$WORKER_REPORT"
  echo "Saved to $WORKER_REPORT"
fi
```

### Step 8: Write E2E Report

File: `docs/e2e_results/<project_name>-<date>.md`

**Never overwrite existing reports** — append suffix: `-2`, `-3`, etc.

Classify each problem by type:

| Type | Meaning |
|------|---------|
| **orchestrator** | Bug in codegen_orchestrator |
| **template** | Bug in `service-template` |
| **meta** | Error in this skill's instructions |
| **other** | Network, transient, hardware |

```markdown
# E2E Report: <project_name> — <brief summary>

> **Date**: YYYY-MM-DD
> **Project**: <project_name> (project_id: `...`)
> **Story**: <story_id>
> **Status**: Passed / Failed
> **Feature phase**: passed / failed / skipped (only if --feature)
> **Smoke**: pass / fail / none
> **Worker audit**: collected | not found

---

## Timeline
(chronological log — key timestamps and events)

## PO Interaction

## Problems Found

### Problem 1: <title>
- **Type**: orchestrator | template | meta | other
- **Severity**: critical / major / minor
- **Backlog**: `#XX` | `new` | `template` | `—`
- **Description**: ...
- **Root cause**: ...
- **Suggested fix**: ...
```

### Steps F1-F4: Feature Add Phase (only if --feature)

Skip unless `--feature` is set AND the initial create+deploy passed.

#### Step F1: Create feature story via PO

Send a feature request message to PO:
"Добавь в мой $PROJECT_NAME: $FEATURE_DESCRIPTION"

Use the same `po:input` / `po:response` mechanism from Step 1b/1c.

Then extract the new story ID:

```bash
FEATURE_STORY_ID=$(curl -s "http://localhost:8000/api/stories/?sort=-created_at" \
  | jq -r --arg pid "$PROJECT_ID" '[.[] | select(.project_id == $pid)] | .[0].id')

echo "FEATURE_STORY_ID=$FEATURE_STORY_ID"
```

#### Step F2: Monitor feature pipeline

Same as Steps 3-5, but for `$FEATURE_STORY_ID`. Scaffold runs in `ensure` mode
(workspace already exists — no copier, just verify).

Verify NO full scaffold ran:
```bash
docker compose logs scaffolder --tail=20 --since=5m 2>/dev/null | grep -E "mode|copier"
```

#### Step F3: Verify feature

Same as Step 6, plus verify the specific feature from Feature Add Matrix:
- `todo_api`: `curl -sf http://$SERVER_IP:$DEPLOY_PORT/todos/stats | jq .`
- `weather_bot`: `curl -sf http://$SERVER_IP:$DEPLOY_PORT/api/forecast/moscow | jq .`

#### Step F4: Collect feature reports

Same as Step 7 — fetch worker reports and audit report for the feature story.

### Step 8.5: Commit reports

```bash
git add docs/e2e_results/
git commit -m "e2e: $PROJECT_NAME — <pass/fail>"
```

Do NOT push.

### Step 9: Cleanup (skip if --no-cleanup)

```bash
# 1. Kill worker containers
docker ps --filter "label=com.codegen.type=worker" --format "{{.Names}}" | xargs -r docker rm -f

# 2. Delete GitHub repo
docker compose exec -T api python -c "
import asyncio
from shared.clients.github import GitHubAppClient
async def main():
    gh = GitHubAppClient()
    await gh.delete_repo('project-factory-organization', '$REPO_SLUG')
    print('Repo deleted')
asyncio.run(main())
"

# 3. Clean server deployment
if [ -n "$SERVER_IP" ]; then
  bash infra/scripts/ssh-to-server.sh $SERVER_IP "
    if [ -d /opt/services/$PROJECT_NAME/infra ]; then
      cd /opt/services/$PROJECT_NAME/infra
      docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml down -v --remove-orphans 2>/dev/null || true
    fi
    rm -rf /opt/services/$PROJECT_NAME
    echo 'Server cleanup done'
  " || echo "WARNING: SSH cleanup failed"
fi

# 4. Delete deployment records
curl -s "http://localhost:8000/api/service-deployments/?project_id=$PROJECT_ID" \
  | jq -r '.[].id' | while read ID; do
    curl -s -X DELETE "http://localhost:8000/api/service-deployments/$ID"
  done

# 5. Delete project from DB via SQL (API endpoint doesn't cascade stories)
docker compose exec -T db psql -U postgres -d orchestrator -c "
  BEGIN;
  DELETE FROM task_events WHERE task_id IN (SELECT id FROM tasks WHERE story_id IN (SELECT id FROM stories WHERE project_id = '$PROJECT_ID'));
  DELETE FROM runs WHERE project_id = '$PROJECT_ID';
  DELETE FROM tasks WHERE story_id IN (SELECT id FROM stories WHERE project_id = '$PROJECT_ID');
  DELETE FROM stories WHERE project_id = '$PROJECT_ID';
  DELETE FROM port_allocations WHERE application_id IN (SELECT id FROM applications WHERE repo_id IN (SELECT id FROM repositories WHERE project_id = '$PROJECT_ID'));
  DELETE FROM applications WHERE repo_id IN (SELECT id FROM repositories WHERE project_id = '$PROJECT_ID');
  DELETE FROM repositories WHERE project_id = '$PROJECT_ID';
  DELETE FROM service_deployments WHERE project_id = '$PROJECT_ID';
  DELETE FROM projects WHERE id = '$PROJECT_ID';
  COMMIT;
"

# 6. Trim stale messages from all queues
docker compose exec -T api python3 -c "
import asyncio
import redis.asyncio as redis

async def main():
    r = redis.from_url('redis://redis:6379')
    for q in ['scaffold:queue', 'architect:queue', 'engineering:queue',
              'deploy:queue', 'worker:commands', 'po:input', 'po:proactive']:
        length = await r.xlen(q)
        if length > 0:
            await r.xtrim(q, maxlen=0)
            print(f'{q}: trimmed {length} messages')
    # Clean stale po:response streams
    async for key in r.scan_iter('po:response:*'):
        await r.delete(key)
        print(f'Deleted {key.decode()}')
    # Clean scaffold inflight keys
    async for key in r.scan_iter('scaffold:inflight:*'):
        await r.delete(key)
        print(f'Deleted {key.decode()}')
    await r.aclose()

asyncio.run(main())
"

# 7. Clean PO checkpoint for e2e test user
docker compose exec -T db psql -U postgres -d orchestrator -c "
  DELETE FROM langgraph.checkpoint_writes WHERE thread_id = 'po-user-999000001';
  DELETE FROM langgraph.checkpoint_blobs WHERE thread_id = 'po-user-999000001';
  DELETE FROM langgraph.checkpoints WHERE thread_id = 'po-user-999000001';
"
```

## Final Summary

After all tests complete, print a summary table:

```
## E2E Test Results

| # | Project | Create | Feature | Duration | Problems |
|---|---------|--------|---------|----------|----------|
| 1 | todo_api | PASS | PASS | 25min | 0 |
| 2 | weather_bot | FAIL | SKIP | 18min | 2 |

Total: X passed, Y failed out of Z tests
```

## Error Handling & Light Interventions

**What you CAN do** (light interventions):
- Transition stuck story/task statuses (e.g., `POST .../start`)
- Clean stale queue messages
- Clear stale Redis keys (inflight markers)
- Retry failed tasks (transition back to `todo`)

**What you should NOT do**:
- Write or fix code
- Clone repos and push commits
- Create tasks manually
- Restart orchestrator services

If something needs a heavy intervention to proceed, record it as a finding and move on.

**General rules**:
- If a step fails, document and continue to the next step
- Do NOT stop the entire run on a single failure — collect as much data as possible
- If the API is unreachable, STOP — the stack is down
- Always attempt cleanup even if the test failed (unless --no-cleanup)

## Abort & Collect

If the user asks to stop early:

1. Kill worker containers
2. Collect whatever data is available (Steps 6-7)
3. Write report with "Failed (aborted)"
4. Cleanup (unless --no-cleanup)

## Common Gotchas

1. **Architect is its own container** — `docker compose logs architect`, NOT scheduler
2. **Repo names use hyphens, not underscores**: `todo_api` → `todo-api`
3. **Local `gh` CLI has no access** — always use `GitHubAppClient` via docker compose exec
4. **Import path**: `from shared.clients.github import GitHubAppClient`
5. **Worker containers**: use introspection API (`/wm-api/workers/`) or `docker ps` with label filter
6. **Story must be `in_progress` for deploy to trigger** — nudge with `POST .../start` if stuck
7. **Story transitions are action-based** — `POST /start`, `/complete`, NOT PATCH
8. **Stale queue messages** can clog architect for hours — check queues early
9. **Project needs a Repository record** — scaffold_trigger won't fire without it
10. **Cross-check sources** — compare API data with Docker/Redis. Desync is a finding.

## Self-Feedback (Mandatory)

Before generating your final response, check: did you encounter wrong commands,
missing info, or unexpected errors? If yes, append to `docs/skill-feedback.md`:

```markdown
## [e2e-run] — <today's date>
- **Type**: bug | missing-info | optimization
- **Quote**: "<exact line from this skill>"
- **Problem**: <what went wrong>
- **Suggested fix**: <concrete change>
```
