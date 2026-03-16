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

## Worker Audit

Workers already write `/workspace/REPORT.md` (per INSTRUCTIONS.md) with Issues Encountered
and Suggestions sections. This IS the audit report — no separate AUDIT_REPORT.md needed.
Worker reports are collected via task events API (step 7a).

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
  → LLM decomposes story into Tasks
  ↓
Task Dispatcher (scheduler, 30s cycle)
  → finds TODO tasks with no blockers
  → creates Run record, publishes EngineeringMessage to engineering:queue
  → task: TODO → IN_DEV
  ↓
Engineering Worker (langgraph container, separate entrypoint)
  → spawns worker container via worker-manager
  → worker runs Claude CLI agent on story/{story_id} branch
  → agent commits, pushes to feature branch
  → task: IN_DEV → DONE (or FAILED)
  ↓
All tasks DONE → Dispatcher creates PR story/{id} → main
  → enables auto-merge
  → story: IN_PROGRESS → PR_REVIEW
  ↓
CI runs on PR → green → auto-merge → webhook (pull_request merged)
  → story: PR_REVIEW → DEPLOYING
  → publishes DeployMessage to deploy:queue
  (Red CI → webhook creates fix task → story back to IN_PROGRESS)
  ↓
Deploy Worker (langgraph container, separate entrypoint)
  → configures GitHub secrets
  → triggers deploy.yml workflow
  → runs smoke test
  → story: DEPLOYING → TESTING
  → publishes QAMessage to qa:queue
  ↓
QA Consumer (langgraph container, separate entrypoint)
  → SSHes to prod server as root
  → runs Claude Code CLI with QA prompt (tests deployed project as real user)
  → Claude Code tests endpoints, checks responses, validates against story
  → story: TESTING → COMPLETED (pass) or back to IN_PROGRESS (fail → fix task)
```

**Key containers** (each is separate in docker-compose):
- `langgraph` — PO agent
- `architect` — story decomposition (NOT inside scheduler!)
- `scheduler` — scaffold trigger, task dispatcher, story completion
- `engineering-worker` — engineering consumer
- `deploy-worker` — deploy consumer
- `qa-worker` — QA consumer (post-deploy testing via Claude Code on prod server)
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

**Server filesystem layout**: `/opt/services/<REPO_SLUG>/` (hyphenated name, e.g. `weather-bot`)

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
Use `$REPO_SLUG` for GitHub API **and server paths** (`/opt/services/$REPO_SLUG`).
Use `$PROJECT_NAME` only for API/DB queries and local file names.

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

# 3. Clean stale deployments on servers (check BOTH underscore and hyphen variants)
for SERVER_IP in $(curl -s "http://localhost:8000/api/servers/?is_managed=true" | jq -r '.[].public_ip'); do
  for DIR_NAME in "$PROJECT_NAME" "$REPO_SLUG"; do
    HAS_DIR=$(bash infra/scripts/ssh-to-server.sh $SERVER_IP \
      "[ -d /opt/services/$DIR_NAME ] && echo EXISTS || echo CLEAN" 2>/dev/null || echo "SSH_FAIL")
    if [ "$HAS_DIR" = "EXISTS" ]; then
      echo "WARNING: Stale deployment $DIR_NAME on $SERVER_IP — cleaning"
      bash infra/scripts/ssh-to-server.sh $SERVER_IP "
        cd /opt/services/$DIR_NAME/infra 2>/dev/null && \
          docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml down -v --remove-orphans 2>/dev/null || true
        rm -rf /opt/services/$DIR_NAME
      "
    fi
  done
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
    for q in ['architect:queue', 'engineering:queue', 'deploy:queue', 'scaffold:queue', 'qa:queue']:
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

PO may create the project with a hyphenated name (`weather-bot`) even if you said `weather_bot`.
Search by both variants:

```bash
REPO_SLUG=$(echo "$PROJECT_NAME" | tr '_' '-')

PROJECT_ID=$(curl -s "http://localhost:8000/api/projects/" \
  | jq -r --arg name "$PROJECT_NAME" --arg slug "$REPO_SLUG" \
    '.[] | select(.name == $name or .name == $slug) | .id' | head -1)

STORY_ID=$(curl -s "http://localhost:8000/api/stories/?sort=-created_at" \
  | jq -r --arg pid "$PROJECT_ID" '.[] | select(.project_id == $pid) | .id' | head -1)

REPO_ID=$(curl -s "http://localhost:8000/api/projects/$PROJECT_ID" \
  | jq -r '.repositories[0].id // empty')

echo "PROJECT_ID=$PROJECT_ID STORY_ID=$STORY_ID REPO_ID=$REPO_ID"
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

**Verify**: Task chain should have sensible descriptions and correct blocking order.

**Post-architect check**: After tasks appear, verify the first unblocked task transitions
to `in_dev` within 2 minutes. If it stays `todo`:

```bash
# Check dispatcher is running and picking up tasks
docker compose logs scheduler --since=2m 2>/dev/null | grep -i dispatch | tail -10

# Check engineering:queue — was a message published?
curl -s "http://localhost:8000/debug/queues/engineering:queue/messages?count=10" | python3 -c "
import json, sys
data = json.load(sys.stdin)
print(f'Engineering queue: {data[\"total\"]} messages')
for m in data['messages']:
    print(f\"  {m['id']}  task={m['data'].get('task_id','?')}\")
"
```

If no dispatch after 2 min, the dispatcher may be stuck or the task isn't in `todo` status.

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
- Check via Docker (primary — most reliable):
  ```bash
  docker ps --filter "label=com.codegen.type=worker" --format "{{.Names}}\t{{.Status}}"
  ```
- Worker logs (via docker directly):
  ```bash
  WORKER_CONTAINER=$(docker ps --filter "label=com.codegen.type=worker" --format "{{.Names}}" | head -1)
  docker logs "$WORKER_CONTAINER" --tail=20 2>&1
  ```
- Worker-Manager API may also work, but handle errors (can return 404 or non-list):
  ```bash
  curl -s http://localhost:8000/wm-api/workers/ | python3 -c "
  import json, sys
  data = json.load(sys.stdin)
  if isinstance(data, list):
      for w in data:
          print(f\"{w['container_name']}  status={w['status']}  task={w.get('task_id','?')}\")
  else:
      print(f'WM API error: {data}')
  " 2>/dev/null || echo "WM API unavailable"
  ```

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

**Timeouts by phase** (only workers take a long time — everything else should be fast):
- **Scaffold**: 5 min max (poll every 15s)
- **Architect**: 5 min max (poll every 10s)
- **Engineering worker**: 30 min per task (poll every 30s) — this is the only long phase
- **PR merge / auto-merge**: 2 min max (poll every 15s). If stuck, check and intervene
- **Deploy**: 5 min max (poll every 15s). Deploy-worker triggers GH Actions then waits

If a non-worker phase exceeds its timeout, don't keep waiting — investigate immediately.

Keep a timeline log:
```
HH:MM  task-xxx  todo → in_dev (worker started)
HH:MM  task-xxx  in_dev → in_ci (commit pushed)
HH:MM  task-xxx  CI passed → done
```

### Step 5: Monitor PR Review & Deploy

When all tasks are done, the dispatcher creates a PR from `story/{story_id}` → `main`,
enables auto-merge, and transitions the story to `pr_review`. Deploy is triggered later
by the webhook when the PR is merged (after CI passes on the PR).

**Flow**: `in_progress` → `pr_review` → (PR merged via webhook) → `deploying` → `completed`

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
POST /api/stories/{id}/deploy    → in_progress/pr_review → deploying
POST /api/stories/{id}/complete  → in_progress/deploying → completed
POST /api/stories/{id}/fail      → any → failed
POST /api/stories/{id}/reopen    → completed/failed → in_progress
```

### Webhook failure & manual deploy trigger

**Known issue**: For newly scaffolded repos, the GitHub webhook may not fire after PR merge.
If story stays in `pr_review` for >60s after merge, the webhook didn't arrive.

**Workaround — manual deploy trigger**:

```bash
import uuid
RUN_ID="deploy-e2e-$(uuid.uuid4().hex[:8])"

# 1. Create Run record (field is "type", NOT "run_type")
curl -s -X POST "http://localhost:8000/api/runs/" \
  -H "Content-Type: application/json" \
  -d "{
    \"id\": \"$RUN_ID\",
    \"project_id\": \"$PROJECT_ID\",
    \"story_id\": \"$STORY_ID\",
    \"type\": \"deploy\",
    \"status\": \"pending\"
  }"

# 2. Transition story to deploying
curl -s -X POST "http://localhost:8000/api/stories/$STORY_ID/deploy" \
  -H "Content-Type: application/json" \
  -d '{"actor": "e2e-test"}'

# 3. Publish deploy message to queue
docker compose exec -T -e "PROJECT_ID=$PROJECT_ID" -e "STORY_ID=$STORY_ID" -e "RUN_ID=$RUN_ID" api python -c "
import os, asyncio
import redis.asyncio as redis
from shared.contracts.queues.deploy import DeployMessage, DeployTrigger
from shared.queues import DEPLOY_QUEUE

async def main():
    r = redis.from_url('redis://redis:6379')
    deploy_msg = DeployMessage(
        task_id=os.environ['RUN_ID'],
        project_id=os.environ['PROJECT_ID'],
        user_id='',
        story_id=os.environ['STORY_ID'],
        triggered_by=DeployTrigger.WEBHOOK,
        action='create',
    )
    mid = await r.xadd(DEPLOY_QUEUE, {'data': deploy_msg.model_dump_json()})
    print(f'Published to deploy:queue: mid={mid}')
    await r.aclose()

asyncio.run(main())
"
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

Poll story status every 15s, timeout after 5 minutes (deploy itself runs on GitHub Actions,
the deploy-worker just triggers it and waits). If no progress after 5 min, check deploy-worker logs.
Story goes through: `pr_review` → `deploying` → `testing` → `completed` (or `failed`).

### Step 5.5: Monitor QA Phase

After deploy succeeds, the deploy-worker publishes a `QAMessage` to `qa:queue` and
transitions the story to `testing`. The QA consumer (`qa-worker` container) SSHes to the
prod server and runs Claude Code CLI to test the deployed project as a real user would.

```bash
# Check story entered testing
curl -s http://localhost:8000/api/stories/$STORY_ID | python3 -c "
import json, sys
s = json.load(sys.stdin)
print(f\"Story: {s['status']}\")
"

# QA worker logs
docker compose logs qa-worker --tail=50 --since=5m 2>/dev/null | grep -v "HTTP Request" | tail -20

# Check qa:queue
curl -s "http://localhost:8000/debug/queues/qa:queue/messages?count=10" | python3 -m json.tool
```

**What to watch**:
- QA consumer picks up the message from `qa:queue`
- SSH to prod server succeeds
- Claude Code runs the QA prompt (tests endpoints, checks responses)
- QA result is parsed (JSON with `pass`, `checks`, `summary`)
- Story transitions to `completed` (if QA passed) or back to `in_progress` (if failed, creates fix task)

**Timeouts**: QA has a 20-minute timeout per run. Poll story status every 30s.

**If QA fails**: Check qa-worker logs for the reason. Common issues:
- SSH connection failed (server unreachable, credentials expired)
- Claude Code not installed on server (run `qa_runner` Ansible role)
- Claude Code session expired (re-copy `.credentials.json`)
- QA prompt produced unparseable output (non-JSON response)

**If QA is stuck**: Check if the qa:queue message was consumed:
```bash
curl -s "http://localhost:8000/debug/queues/qa:queue/qa-consumers/pending" | python3 -m json.tool
```

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
  cd /opt/services/$REPO_SLUG/infra
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
  PROJECT_DIR=/opt/services/$REPO_SLUG
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

**IMPORTANT**: This step MUST complete and save files BEFORE Step 9 cleanup.
Cleanup deletes task_events from DB — if reports aren't saved to disk first, they're lost.

```bash
mkdir -p docs/e2e_results/worker_reports
DATE=$(date +%Y%m%d)
WORKER_REPORT="docs/e2e_results/worker_reports/${PROJECT_NAME}-${DATE}-worker.md"

# Collect all worker reports from task events and save to file
echo "# Worker Reports: ${PROJECT_NAME}" > "$WORKER_REPORT"
echo "" >> "$WORKER_REPORT"

FOUND_REPORTS=0
for TASK_ID in $(curl -s "http://localhost:8000/api/tasks/?story_id=$STORY_ID" | python3 -c "
import json, sys
for t in json.load(sys.stdin):
    print(t['id'])
"); do
  REPORT=$(curl -s "http://localhost:8000/api/tasks/$TASK_ID/events?event_type=worker_report" | python3 -c "
import json, sys
events = json.load(sys.stdin)
for e in events:
    report = e.get('details', {}).get('report', '')
    if report: print(report)
")
  if [ -n "$REPORT" ]; then
    echo "=== Task: $TASK_ID ===" >> "$WORKER_REPORT"
    echo "$REPORT" >> "$WORKER_REPORT"
    echo "" >> "$WORKER_REPORT"
    FOUND_REPORTS=$((FOUND_REPORTS + 1))
  fi
done

if [ "$FOUND_REPORTS" -eq 0 ]; then
  echo "(no worker reports found)" >> "$WORKER_REPORT"
  echo "WARNING: No worker reports collected"
else
  echo "Saved $FOUND_REPORTS worker report(s) to $WORKER_REPORT"
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
> **Worker reports**: collected (N) | none

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

Same as Step 7 — fetch worker reports from task events for the feature story.
Save to `docs/e2e_results/worker_reports/${PROJECT_NAME}-${DATE}-feature-worker.md`.

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

# 3. Clean server deployment (check both underscore and hyphen variants)
if [ -n "$SERVER_IP" ]; then
  for DIR_NAME in "$PROJECT_NAME" "$REPO_SLUG"; do
    bash infra/scripts/ssh-to-server.sh $SERVER_IP "
      if [ -d /opt/services/$DIR_NAME/infra ]; then
        cd /opt/services/$DIR_NAME/infra
        docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml down -v --remove-orphans 2>/dev/null || true
      fi
      rm -rf /opt/services/$DIR_NAME
    " 2>/dev/null || true
  done
  echo "Server cleanup done"
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

# 8. Delete e2e test user from DB
docker compose exec -T db psql -U postgres -d orchestrator -c "
  DELETE FROM users WHERE telegram_id = 999000001;
"

# 9. Delete local workspaces for this project's repos
# $REPO_ID was captured in step 1d (before DB cleanup in step 5).
docker run --rm -v /data/workspaces:/workspaces alpine sh -c "rm -rf /workspaces/$REPO_ID"

# 10. Clean up worker sidecar containers and dev networks
docker ps -a --filter "name=worker_" --format "{{.Names}}" | xargs -r docker rm -f 2>/dev/null || true
docker network ls --filter "name=dev_proj" --format "{{.Name}}" | xargs -r docker network rm 2>/dev/null || true
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
5. **Worker containers**: use `docker ps --filter "label=com.codegen.type=worker"` (primary). WM API (`/wm-api/workers/`) may return 404 or non-list — always handle errors
6. **Story must be `in_progress` for PR creation** — nudge with `POST .../start` if stuck. After PR, story goes to `pr_review`. Deploy triggers via webhook after PR merge
7. **Story transitions are action-based** — `POST /start`, `/complete`, NOT PATCH
8. **Stale queue messages** can clog architect for hours — check queues early
9. **Project needs a Repository record** — scaffold_trigger won't fire without it
10. **Cross-check sources** — compare API data with Docker/Redis. Desync is a finding.
11. **Webhook may not fire for new repos** — if story stays `pr_review` >60s after merge, use the manual deploy trigger recipe (see "Webhook failure" in Step 5).
12. **Deploy Run record uses `type` field** (not `run_type`) — `POST /api/runs/` with `{"type": "deploy"}`.
13. **DeployMessage requires `task_id`** — this is actually the Run ID (format `deploy-e2e-{hex}`), not a task ID.
14. **QA phase after deploy** — story goes `deploying` → `testing` → `completed`. The `qa-worker` container SSHes to prod server and runs Claude Code CLI. If QA fails, story goes back to `in_progress` with a fix task.
15. **QA node prerequisites** — prod server must have Claude Code CLI installed + `.credentials.json` session + 2GB swap. Provisioned via `qa_runner` Ansible role.

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
