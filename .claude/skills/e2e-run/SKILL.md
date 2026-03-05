---
name: e2e-run
description: Run Line 2 E2E test — submit engineering task, wait for completion, verify, write report. Use when user wants to test the engineering pipeline end-to-end.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "<test> [--with-po] [--no-cleanup] [--no-nuke]"
---

# E2E Engineering Test Runner

Run one or more Line 2 E2E tests end-to-end: create project, trigger engineering,
monitor progress, verify results (including deploy), collect audit report, write investigation report, cleanup.

## Arguments

- `$0` — test selector (REQUIRED):
  - Project name: `todo_api`, `echo_bot`, `landing_page`, etc.
  - Test number: `1`, `2`, `3`, etc.
  - Comma-separated: `1,3,5` or `todo_api,echo_bot`
  - `all` — run all 7 tests sequentially
- `--with-po` — route through PO agent instead of direct API/queue calls. Creates test user,
  sends project description to `po:input`, waits for PO to create project & trigger engineering.
- `--no-cleanup` — skip cleanup after test (keep repo, containers, DB records)
- `--no-nuke` — skip `make nuke` in Step 0 (assume stack is already clean and running)

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

Use this format for each finding:

### <number>. <title>
- **Severity**: critical / major / minor
- **Category**: framework | template | tooling | docs
- **File**: <path> (if applicable)
- **Description**: What happened, what you expected, exact error messages
- **Suggestion**: How to fix or improve

Also include a ## What Worked Well section with positive observations.
```

## E2E Secrets (for tg_bot tests)

Tests with `tg_bot` module (#2 echo_bot, #4 weather_bot, #6 bot_landing, #7 expense_tracker)
need a `TELEGRAM_BOT_TOKEN` for deploy. Tests without `tg_bot` work without it.

Secrets are read from `.claude/e2e-secrets.env` (gitignored). If missing, tell the user:

```
Create .claude/e2e-secrets.env (see .claude/e2e-secrets.env.example).
A test bot token from @BotFather is needed for tg_bot tests.
```

**Injection**: After creating the project (Step 1 or after PO creates it in Step 1-PO),
if the test includes `tg_bot`, inject secrets into `project.config.secrets`:

```bash
# Read token from secrets file
TG_TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' .claude/e2e-secrets.env 2>/dev/null | cut -d= -f2-)

if [ -z "$TG_TOKEN" ]; then
  echo "ERROR: TELEGRAM_BOT_TOKEN not found in .claude/e2e-secrets.env"
  echo "tg_bot tests require a bot token. See .claude/e2e-secrets.env.example"
  # STOP this test — cannot deploy without token
fi

# Inject encrypted secret into project config
docker compose exec -T -e "TG_TOKEN=$TG_TOKEN" -e "PROJECT_ID=$PROJECT_ID" langgraph python -c "
import os, asyncio
import httpx
from shared.crypto import encrypt_dict

async def main():
    pid = os.environ['PROJECT_ID']
    token = os.environ['TG_TOKEN']
    async with httpx.AsyncClient(base_url='http://api:8000') as api:
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

Skip this step entirely for tests without `tg_bot` module.

## GitHub Access

**IMPORTANT**: The local `gh` CLI is bound to a personal account that does NOT have access
to `project-factory-organization` repos. Never use `gh api`, `gh run`, `gh repo delete`, etc.

Instead, use `GitHubAppClient` via docker compose exec. The `langgraph` container has the
GitHub App credentials mounted and all necessary methods:

```bash
# Helper: run GitHubAppClient methods from the host
docker compose exec -T langgraph python -c "
import asyncio
from shared.clients.github import GitHubAppClient

async def main():
    gh = GitHubAppClient()
    # Available methods:
    # gh.list_repo_files(owner, repo, path='', ref='main') -> list[str]
    # gh.get_file_contents(owner, repo, path, ref='main') -> str | None
    # gh.get_latest_workflow_run(owner, repo, workflow_file, branch, created_after=None) -> dict
    # gh.delete_repo(owner, repo) -> None
    # gh.create_repo(org, name, description, private) -> dict
    result = await gh.list_repo_files('project-factory-organization', 'REPO_NAME')
    print(result)

asyncio.run(main())
"
```

Use this pattern everywhere you need to interact with GitHub repos.

## Server Access

Deployed services run on managed VPS servers. Projects are allocated to servers
during engineering via the resource allocator. The deploy workflow SSHes into the
server and runs `docker compose up`.

**SSH connection**: Connect as `root` using the local SSH key. Servers may be
reprovisioned (new OS install), which changes their host keys. Always use
`-o StrictHostKeyChecking=accept-new` to auto-accept new keys. If SSH fails with
"REMOTE HOST IDENTIFICATION HAS CHANGED", remove the old key and retry:

```bash
ssh-keygen -f "$HOME/.ssh/known_hosts" -R "$SERVER_IP"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 root@$SERVER_IP "hostname"
```

**Finding the server IP and port**: The deploy task result contains the `deployed_url`
(e.g. `http://1.2.3.4:8000`) — this is the most reliable source. For failed deploys
where `deployed_url` is absent, resolve IP from `service-deployments` or the server's
`public_ip` field via port allocation handle lookup.

```bash
# Option 1 (preferred): from deploy task result — always has IP:port if deploy ran
DEPLOY_RESULT=$(curl -s "http://localhost:8000/api/tasks/$DEPLOY_TASK")
DEPLOYED_URL=$(echo "$DEPLOY_RESULT" | jq -r '.result.deployed_url // empty')
if [ -n "$DEPLOYED_URL" ]; then
  SERVER_IP=$(echo "$DEPLOYED_URL" | sed -E 's|https?://([^:/]+).*|\1|')
  DEPLOY_PORT=$(echo "$DEPLOYED_URL" | sed -E 's|.*:([0-9]+)$|\1|')
fi

# Option 2: from service-deployments (only if deploy succeeded)
SERVER_IP=$(curl -s "http://localhost:8000/api/service-deployments/?project_id=$PROJECT_ID" | jq -r '.[0].server_ip // empty')

# Option 3: resolve via port allocations → server handle → server public_ip
SERVER_HANDLE=$(curl -s "http://localhost:8000/api/servers/?is_managed=true" \
  | jq -r '.[].handle' \
  | while read h; do
      HAS=$(curl -s "http://localhost:8000/api/servers/$h/ports" \
        | jq -r --arg pid "$PROJECT_ID" '[.[] | select(.project_id == $pid)] | length')
      [ "$HAS" != "0" ] && echo "$h" && break
    done | head -1)
if [ -n "$SERVER_HANDLE" ]; then
  SERVER_IP=$(curl -s "http://localhost:8000/api/servers/$SERVER_HANDLE" | jq -r '.public_ip // empty')
  DEPLOY_PORT=$(curl -s "http://localhost:8000/api/servers/$SERVER_HANDLE/ports" \
    | jq -r --arg pid "$PROJECT_ID" '.[] | select(.project_id == $pid) | .port // empty' | head -1)
fi
```

**Server filesystem layout**: Deployed projects live under `/opt/services/`:

```
/opt/services/<PROJECT_NAME>/
├── .env                    # decoded from DOTENV_B64 secret
├── .env.bak                # backup of previous .env (if redeployed)
└── infra/
    ├── compose.base.yml    # base compose (services, volumes, healthchecks)
    └── compose.prod.yml    # prod overlay (image refs, restart policy)
```

**Docker compose on server**: Always use both compose files and the env file:

```bash
COMPOSE="docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml"
$COMPOSE ps -a
$COMPOSE logs backend --tail=50
$COMPOSE down -v --remove-orphans
```

## Execution Flow

For each selected test case, execute these steps. If running multiple tests,
run them **sequentially** (one at a time — worker-manager handles one container at a time).

**IMPORTANT — repo naming convention**: GitHub repos use **hyphens**, not underscores.
Always define `REPO_SLUG=$(echo "$PROJECT_NAME" | tr '_' '-')` early and use `$REPO_SLUG`
in ALL GitHub API calls (`list_repo_files`, `get_file_contents`, `get_latest_workflow_run`,
`delete_repo`, raw API URLs). Use `$PROJECT_NAME` only for API calls, DB records, and
server paths (`/opt/services/$PROJECT_NAME`).

### Step 0: Health check + pre-flight cleanup

Before the first test, do a full stack reset to ensure clean state.

**Skip this if `--no-nuke` is set** — go straight to the health check below.

```bash
make nuke
```

Wait 140 seconds for all services to fully initialize (DB migrations, Redis, workers):

```bash
sleep 140
```

Then verify the stack is healthy:

```bash
curl -sf http://localhost:8000/health | jq .
docker compose ps --format "{{.Name}} {{.Status}}" | grep -v "Up"
```

If API is not healthy, STOP and tell the user to fix the stack first.

**Worker image staleness check** (run before the first test):

```bash
CURRENT_HASH=$(find shared packages/worker-wrapper packages/orchestrator-cli \
  services/worker-manager/images -type f \
  -not -path '*/__pycache__/*' -not -name '*.pyc' \
  | LC_ALL=C sort | xargs sha256sum 2>/dev/null | sha256sum | cut -c1-16)

STORED_HASH=$(docker inspect worker-base-common:latest \
  --format '{{index .Config.Labels "org.codegen.worker_source_hash"}}' 2>/dev/null || echo "none")

if [ "$CURRENT_HASH" != "$STORED_HASH" ]; then
  echo "Worker images stale ($STORED_HASH -> $CURRENT_HASH) — rebuilding..."
  make rebuild-worker-images
else
  echo "Worker images up to date (hash: $CURRENT_HASH)"
fi
```

If rebuild fails, STOP and tell the user. Stale worker images cause persistent bugs
(e.g., POSTGRES_HOST=project-db from deleted _patch_db_hostname).

**Pre-flight: clean up stale artifacts** (run for every test):

```bash
ORG="project-factory-organization"
# Convert PROJECT_NAME to repo slug (underscores → hyphens)
REPO_SLUG=$(echo "$PROJECT_NAME" | tr '_' '-')

# 1. Check and delete leftover GitHub repo
# NOTE: Use `tail -1` to extract only the final print() line.
# structlog errors go to stdout and pollute the captured output otherwise.
REPO_EXISTS=$(docker compose exec -T langgraph python -c "
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
  echo "WARNING: Leftover repo $ORG/$REPO_SLUG found — deleting"
  docker compose exec -T langgraph python -c "
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
docker ps --filter "name=dev-" --format "{{.Names}}" | grep "$REPO_SLUG" | xargs -r docker rm -f
```

Also check target servers for stale deployments:

```bash
# Check all managed servers for leftover /opt/services/<PROJECT_NAME>/
for SERVER_IP in $(curl -s "http://localhost:8000/api/servers/?is_managed=true" | jq -r '.[].public_ip'); do
  HAS_DIR=$(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 root@$SERVER_IP \
    "[ -d /opt/services/$PROJECT_NAME ] && echo EXISTS || echo CLEAN" 2>/dev/null || echo "SSH_FAIL")

  if [ "$HAS_DIR" = "EXISTS" ]; then
    echo "WARNING: Stale deployment /opt/services/$PROJECT_NAME on $SERVER_IP — cleaning"
    ssh -o StrictHostKeyChecking=accept-new root@$SERVER_IP "
      cd /opt/services/$PROJECT_NAME/infra 2>/dev/null && \
        docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml down -v --remove-orphans 2>/dev/null || true
      rm -rf /opt/services/$PROJECT_NAME
    "
  elif [ "$HAS_DIR" = "SSH_FAIL" ]; then
    echo "WARNING: Could not reach $SERVER_IP — skipping server check"
  fi
done
```

If any cleanup happened, log it in the report timeline as "Pre-flight: cleaned stale artifacts".

### Step 1: Create project (direct mode — default)

Skip this step if `--with-po` is set. Go to Step 1-PO instead.

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

**If test includes `tg_bot`** — inject secrets now (see "E2E Secrets" section above).
If secrets file is missing or token is empty, STOP this test.

### Step 2: Trigger engineering (direct mode — default)

Skip this step if `--with-po` is set. Go to Step 2-PO instead.

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
        skip_deploy=False,
        callback_stream='agent:events:manual-test',
    )
    mid = await client.publish_message(ENGINEERING_QUEUE, msg)
    print(f'Published: task={msg.task_id} mid={mid}')
    await client.close()

asyncio.run(main())
"
```

Save `TASK_ID`.

### Step 1-PO: Create test user & send to PO (--with-po mode)

Skip this step unless `--with-po` is set.

**1a. Upsert test user** — ensures `owner_id` will link correctly when #27 is fixed:

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

**1b. Send project request to PO via `po:input`**.

Compose a natural-language message that gives PO enough information to create the project
and trigger engineering without follow-up questions. Include the project name, modules,
full description, and the audit instructions.

The message should look like:

```
Создай проект "<PROJECT_NAME>" с модулями: <MODULES>.

Описание: <DESCRIPTION from matrix>

<AUDIT INSTRUCTIONS block>

После создания сразу запусти engineering.
```

Publish via Redis and wait for response:

```bash
REQUEST_ID=$(python3 -c 'import uuid; print(uuid.uuid4())')
E2E_USER_ID="999000001"

docker compose exec -T \
  -e "REQUEST_ID=$REQUEST_ID" \
  -e "E2E_USER_ID=$E2E_USER_ID" \
  -e "MESSAGE_TEXT=$MESSAGE_TEXT" \
  langgraph python -c "
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
    print(f'Published to po:input: mid={mid}, request_id={msg.request_id}')
    await client.close()

asyncio.run(main())
"
```

**1c. Wait for PO response** (timeout 120s — PO may need to call multiple tools):

```bash
docker compose exec -T -e "REQUEST_ID=$REQUEST_ID" langgraph python -c "
import os, asyncio

async def main():
    import redis.asyncio as redis
    r = redis.from_url('redis://redis:6379')
    request_id = os.environ['REQUEST_ID']
    stream = f'po:response:{request_id}'
    # Poll with XREAD, block 5s at a time, total timeout 120s
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
                    # Cleanup response stream
                    await r.delete(stream)
                    await r.aclose()
                    return
    print('TIMEOUT: PO did not respond within 120s')
    await r.aclose()

asyncio.run(main())
"
```

**1d. Extract PROJECT_ID and TASK_ID**.

After PO responds, retrieve the project and task IDs from the API:

```bash
# Find project by name
PROJECT_ID=$(curl -s "http://localhost:8000/api/projects/" \
  | jq -r --arg name "$PROJECT_NAME" '.[] | select(.name == $name) | .id' | head -1)

# Find engineering task for this project
TASK_ID=$(curl -s "http://localhost:8000/api/tasks/?type=engineering&project_id=$PROJECT_ID" \
  | jq -r '.[0].id // empty')

echo "PROJECT_ID=$PROJECT_ID TASK_ID=$TASK_ID"
```

If either is empty, PO may not have completed both actions. Check the PO response text
for clues. You can send a follow-up message to `po:input` (same `E2E_USER_ID`, new `REQUEST_ID`)
asking PO to proceed. If PO is stuck after 2 attempts, fall back to direct mode (Steps 1+2)
and note "PO failed, fell back to direct" in the report.

**1e. If test includes `tg_bot`** — inject secrets now (see "E2E Secrets" section above).

### Step 2-PO: (no-op)

Engineering was already triggered by PO in Step 1-PO. Proceed to Step 3.

### Step 3: Verify scaffold started (CRITICAL — do immediately!)

Wait 20 seconds, then check worker-manager logs:

```bash
sleep 20
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

Poll task status. Print status updates every check.

**Worker log checks**: During polling, periodically check worker container logs to understand
what the agent is doing (especially useful for long waits):

```bash
# Check worker progress (find container name from Step 3 logs)
docker logs worker-dev-$PROJECT_NAME_SLUG-* --tail=10 2>&1
# Also check engineering-worker for subgraph-level events
docker compose logs engineering-worker --tail=10 --since=120s 2>&1 | grep -v "health"
```

**CI fix tracking**: The worker may go through multiple CI fix cycles (push → CI fail →
fix → push). Track each cycle by fetching all CI runs and commits when the task completes or
when you need to understand progress:

```bash
ORG="project-factory-organization"
# IMPORTANT: GitHub repos use hyphens, not underscores. Always use $REPO_SLUG here.
docker compose exec -T langgraph python -c "
import asyncio
from shared.clients.github import GitHubAppClient
import httpx

async def main():
    gh = GitHubAppClient()
    token = await gh.get_org_token('$ORG')
    async with httpx.AsyncClient() as http:
        # All commits
        r = await http.get(
            'https://api.github.com/repos/$ORG/$REPO_SLUG/commits?per_page=10',
            headers={'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}
        )
        print('=== Commits ===')
        for c in r.json():
            msg = c['commit']['message'].split(chr(10))[0]
            print(f'{c[\"sha\"][:8]} {c[\"commit\"][\"author\"][\"date\"]} {msg}')
        # All CI runs
        r = await http.get(
            'https://api.github.com/repos/$ORG/$REPO_SLUG/actions/runs?per_page=10',
            headers={'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}
        )
        print('=== CI Runs ===')
        for run in r.json().get('workflow_runs', []):
            print(f'Run #{run[\"id\"]}: status={run[\"status\"]}, conclusion={run.get(\"conclusion\")}, created={run[\"created_at\"]}')

asyncio.run(main())
" 2>/dev/null
```

This data is essential for the report — include each CI fix attempt in the Timeline.

**Poll engineering task**:

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

**Then find and poll deploy task**:

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

# Extract smoke_result from deploy task (distinguishes deploy fail vs smoke fail)
DEPLOY_RESULT=$(curl -s http://localhost:8000/api/tasks/$DEPLOY_TASK)
SMOKE_STATUS=$(echo "$DEPLOY_RESULT" | jq -r '.result.smoke_result.status // "none"')
DEPLOYED_URL=$(echo "$DEPLOY_RESULT" | jq -r '.result.deployed_url // empty')
echo "Deploy status: $STATUS, Smoke: $SMOKE_STATUS, URL: $DEPLOYED_URL"

# If deploy task failed but deployed_url exists — it's a smoke failure, not deploy failure
if [ "$STATUS" = "failed" ] && [ -n "$DEPLOYED_URL" ]; then
  echo "NOTE: Deploy succeeded but smoke test failed"
  echo "$DEPLOY_RESULT" | jq '.result.smoke_result.checks[]'
fi
```

**Smoke diagnostics** — check deploy-worker logs for subgraph result details (critical for #25 investigation):

```bash
docker compose logs deploy-worker --since=30m 2>&1 | grep "devops_subgraph_result"
```

This log line shows: `result_keys` (which state fields were returned), `has_smoke_result`,
`smoke_result` value, `errors` (if non-empty, smoke_tester was skipped by routing).
Include the full log line in the report under Timeline or Problems.

### Step 5: Verify

**5a. CI status**:

```bash
ORG="project-factory-organization"
# IMPORTANT: GitHub repos use hyphens, not underscores. Always use $REPO_SLUG here.
docker compose exec -T langgraph python -c "
import asyncio
from shared.clients.github import GitHubAppClient

async def main():
    gh = GitHubAppClient()
    try:
        run = await gh.get_latest_workflow_run('$ORG', '$REPO_SLUG', 'ci.yml', 'main')
        print(f\"CI run #{run['id']}: status={run['status']}, conclusion={run.get('conclusion')}\")
        print(f\"URL: {run['html_url']}\")
    except Exception as e:
        print(f'CI check failed: {e}')

asyncio.run(main())
"
```

**5b. Service running on server**:

First, extract server IP and port from the deploy task result (see "Server Access" section):

```bash
# Extract IP and port from deployed_url (most reliable source)
DEPLOY_RESULT=$(curl -s "http://localhost:8000/api/tasks/$DEPLOY_TASK")
DEPLOYED_URL=$(echo "$DEPLOY_RESULT" | jq -r '.result.deployed_url // empty')
if [ -n "$DEPLOYED_URL" ]; then
  SERVER_IP=$(echo "$DEPLOYED_URL" | sed -E 's|https?://([^:/]+).*|\1|')
  DEPLOY_PORT=$(echo "$DEPLOYED_URL" | sed -E 's|.*:([0-9]+)$|\1|')
fi

# Fallback: service-deployments API
if [ -z "$SERVER_IP" ]; then
  SERVER_IP=$(curl -s "http://localhost:8000/api/service-deployments/?project_id=$PROJECT_ID" | jq -r '.[0].server_ip // empty')
fi

# Fallback: port allocations → server handle → server public_ip
if [ -z "$SERVER_IP" ]; then
  SERVER_HANDLE=$(curl -s "http://localhost:8000/api/servers/?is_managed=true" \
    | jq -r '.[].handle' \
    | while read h; do
        HAS=$(curl -s "http://localhost:8000/api/servers/$h/ports" \
          | jq -r --arg pid "$PROJECT_ID" '[.[] | select(.project_id == $pid)] | length')
        [ "$HAS" != "0" ] && echo "$h" && break
      done | head -1)
  if [ -n "$SERVER_HANDLE" ]; then
    SERVER_IP=$(curl -s "http://localhost:8000/api/servers/$SERVER_HANDLE" | jq -r '.public_ip // empty')
    DEPLOY_PORT=$(curl -s "http://localhost:8000/api/servers/$SERVER_HANDLE/ports" \
      | jq -r --arg pid "$PROJECT_ID" '.[] | select(.project_id == $pid) | .port // empty' | head -1)
  fi
fi
echo "Server: $SERVER_IP, Port: $DEPLOY_PORT"
```

**If deploy succeeded** — verify service is healthy:

```bash
# Check deployment records
curl -s "http://localhost:8000/api/service-deployments/?project_id=$PROJECT_ID" | jq .

# SSH to server and verify containers
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 root@$SERVER_IP "
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

# Read smoke_result from deploy task (automated post-deploy check)
echo "=== Smoke Test Result ==="
curl -s http://localhost:8000/api/tasks/$DEPLOY_TASK | jq '.result.smoke_result // "no smoke result"'

# Independent cross-check: curl the health endpoint directly
curl -sf "http://$SERVER_IP:$DEPLOY_PORT/health" | jq . || echo "Health endpoint not responding"
```

**If deploy failed** — SSH to server and collect crash diagnostics:

```bash
# Fix SSH host key if needed
ssh-keygen -f "$HOME/.ssh/known_hosts" -R "$SERVER_IP" 2>/dev/null
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 root@$SERVER_IP "
  PROJECT_DIR=/opt/services/$PROJECT_NAME
  if [ ! -d \"\$PROJECT_DIR\" ]; then
    echo 'No deployment directory found on server'
    exit 0
  fi

  echo '=== .env contents ==='
  cat \$PROJECT_DIR/.env 2>/dev/null || echo 'NO .env FILE'

  echo '=== Compose files ==='
  ls -la \$PROJECT_DIR/infra/

  cd \$PROJECT_DIR/infra
  COMPOSE='docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml'

  echo '=== Container status ==='
  \$COMPOSE ps -a 2>/dev/null || echo 'No containers'

  echo '=== Backend logs (last 50 lines) ==='
  \$COMPOSE logs backend --tail=50 2>/dev/null || echo 'No backend logs'

  echo '=== DB logs (last 20 lines) ==='
  \$COMPOSE logs db --tail=20 2>/dev/null || echo 'No db logs'

  echo '=== Restart counts ==='
  for cid in \$(\$COMPOSE ps -q 2>/dev/null); do
    restarts=\$(docker inspect --format '{{.RestartCount}}' \"\$cid\")
    name=\$(docker inspect --format '{{.Name}}' \"\$cid\")
    state=\$(docker inspect --format '{{.State.Status}}' \"\$cid\")
    echo \"\$name: state=\$state restarts=\$restarts\"
  done
"
```

The crash diagnostics output is essential for the report — include the error message
and import chain (if applicable) in the Problem description.

### Step 6: Collect worker audit report

Try to fetch `AUDIT_REPORT.md` that the developer worker commits to the repo.

```bash
ORG="project-factory-organization"
DATE=$(date +%Y%m%d)
mkdir -p docs/e2e_results/worker_reports

WORKER_REPORT="docs/e2e_results/worker_reports/${PROJECT_NAME}-${DATE}-worker.md"

# Fetch via GitHubAppClient
docker compose exec -T langgraph python -c "
import asyncio
from shared.clients.github import GitHubAppClient

async def main():
    gh = GitHubAppClient()
    content = await gh.get_file_contents('$ORG', '$REPO_SLUG', 'AUDIT_REPORT.md')
    if content:
        print(content)
    else:
        print('NOT_FOUND')

asyncio.run(main())
" 2>/dev/null > /tmp/audit_report.txt

if grep -q "NOT_FOUND" /tmp/audit_report.txt; then
  echo "No worker audit report found"
else
  cp /tmp/audit_report.txt "$WORKER_REPORT"
  echo "Worker audit report saved to $WORKER_REPORT"
fi
```

### Step 7: Write E2E report

Write your own report to `docs/e2e_results/<project_name>-<date>.md`.

**File naming**: `docs/e2e_results/<project_name>-<date>.md` (one file per test).

**IMPORTANT: Never overwrite existing reports.** If a file with the target name already exists,
append a suffix: `-2`, `-3`, etc. (e.g., `todo_api-20260304-2.md`). Each E2E run
produces a unique report — previous results must be preserved.

Use existing reports in `docs/e2e_results/` as format reference if any exist.

**Worker audit findings → structured Problems.** Read the worker's audit report (saved in Step 6) and include actionable findings as structured entries in `## Problems Found`. The raw worker report is preserved in `docs/e2e_results/worker_reports/` for human reference. The main report is the single source of truth for `/triage`.

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
> **Mode**: direct | with-po
> **Status**: Passed / Failed
> **Smoke**: pass (backend: 200) / fail (tg_bot: timeout) / none (no smoke result)
> **Worker audit**: collected (findings included below) | not found

---

## Timeline
(chronological log of what happened — key timestamps and events)

## PO Interaction (only if --with-po)
(PO response text, whether it created project + triggered engineering on first try,
any follow-up messages needed, fallback to direct if PO failed)

## Problems Found

### Problem 1: <title>
- **Type**: orchestrator | template | meta | other
- **Severity**: critical / major / minor
- **Backlog**: `#XX` (existing orchestrator task) | `new` (for /triage to create) | `template` (for /triage to route to service-template backlog) | `—` (skip, already fixed or not actionable)
- **Description**: ...
- **Root cause**: ...
- **Suggested fix**: ...
```

### Step 7.5: Commit reports

```bash
git add docs/e2e_results/<project_name>-<date>.md
git add docs/e2e_results/worker_reports/ 2>/dev/null || true
git commit -m "e2e: <project_name> — <pass/fail>"
```

### Step 8: Cleanup (skip if --no-cleanup)

```bash
# 1. Kill worker containers
docker ps --filter "name=dev-" --format "{{.Names}}" | grep "$PROJECT_NAME" | xargs -r docker rm -f

# 2. Delete GitHub repo
docker compose exec -T langgraph python -c "
import asyncio
from shared.clients.github import GitHubAppClient

async def main():
    gh = GitHubAppClient()
    await gh.delete_repo('project-factory-organization', '$REPO_SLUG')
    print('Repo deleted')

asyncio.run(main())
"
```

Clean server deployment:

```bash
# 3. Get server IP — try deployed_url first, then service-deployments, then port allocations
DEPLOYMENTS=$(curl -s "http://localhost:8000/api/service-deployments/?project_id=$PROJECT_ID")

# Primary: from deployed_url (already extracted in Step 4/5)
# SERVER_IP should already be set from verification step. If not, re-extract:
if [ -z "$SERVER_IP" ]; then
  DEPLOYED_URL=$(curl -s "http://localhost:8000/api/tasks/$DEPLOY_TASK" | jq -r '.result.deployed_url // empty')
  [ -n "$DEPLOYED_URL" ] && SERVER_IP=$(echo "$DEPLOYED_URL" | sed -E 's|https?://([^:/]+).*|\1|')
fi

# Fallback: service-deployments
if [ -z "$SERVER_IP" ]; then
  SERVER_IP=$(echo "$DEPLOYMENTS" | jq -r '.[0].server_ip // empty')
fi

# Fallback: port allocations → server handle → server public_ip
if [ -z "$SERVER_IP" ]; then
  SERVER_HANDLE=$(curl -s "http://localhost:8000/api/servers/?is_managed=true" \
    | jq -r '.[].handle' \
    | while read h; do
        HAS=$(curl -s "http://localhost:8000/api/servers/$h/ports" \
          | jq -r --arg pid "$PROJECT_ID" '[.[] | select(.project_id == $pid)] | length')
        [ "$HAS" != "0" ] && echo "$h" && break
      done | head -1)
  [ -n "$SERVER_HANDLE" ] && SERVER_IP=$(curl -s "http://localhost:8000/api/servers/$SERVER_HANDLE" | jq -r '.public_ip // empty')
fi

# 4. Remove app from server
if [ -n "$SERVER_IP" ]; then
  ssh-keygen -f "$HOME/.ssh/known_hosts" -R "$SERVER_IP" 2>/dev/null
  ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 root@$SERVER_IP "
    if [ -d /opt/services/$PROJECT_NAME/infra ]; then
      cd /opt/services/$PROJECT_NAME/infra
      docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml down -v --remove-orphans 2>/dev/null || true
    fi
    rm -rf /opt/services/$PROJECT_NAME
    echo 'Server cleanup done'
  " || echo "WARNING: SSH cleanup failed for $SERVER_IP — may need manual cleanup"
fi

# 5. Delete deployment records
echo "$DEPLOYMENTS" | jq -r '.[].id' | while read ID; do
  curl -s -X DELETE "http://localhost:8000/api/service-deployments/$ID"
done
```

Delete project from DB last (cascades tasks + port allocations):

```bash
# 6. Delete project from DB
curl -s -X DELETE http://localhost:8000/api/projects/$PROJECT_ID
```

**If `--with-po`** — also clean the PO thread checkpoint to avoid polluting future tests:

```bash
# 7. Delete PO thread checkpoint (optional, prevents state leaking between tests)
docker compose exec -T langgraph python -c "
import asyncio

async def main():
    from shared.database import get_engine
    from sqlalchemy import text
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text(\"DELETE FROM checkpoints WHERE thread_id = 'po-user-999000001'\"))
        await conn.execute(text(\"DELETE FROM checkpoint_writes WHERE thread_id = 'po-user-999000001'\"))
        print('PO thread checkpoint cleaned')

asyncio.run(main())
" 2>/dev/null || echo "WARNING: Could not clean PO checkpoint (table may not exist)"
```

## Final Summary

After all tests complete, print a summary table:

```
## E2E Test Results

| # | Project | Mode | Status | Duration | Problems | Audit |
|---|---------|------|--------|----------|----------|-------|
| 1 | todo_api | direct | PASS | 25min | 0 | Yes |
| 2 | echo_bot | with-po | FAIL | 18min | 2 | No |
...

Total: X passed, Y failed out of Z tests
```

## Error Handling

- If a step fails, **document the failure** in the report and continue to the next step.
- Do NOT stop the entire run on a single failure — collect as much data as possible.
- If scaffold is skipped, abort that specific test but continue to the next test in batch.
- If the API is unreachable, STOP everything — the stack is down.
- Always attempt cleanup even if the test failed (unless --no-cleanup).

## Abort & Collect (manual interruption)

If the user asks to stop a running test early ("тормози", "stop", "abort"), follow this procedure:

1. **Kill the worker container** immediately:
   ```bash
   docker ps --filter "name=dev-" --format "{{.Names}}" | grep "$PROJECT_NAME" | xargs -r docker rm -f
   ```

2. **Stop the background poller** (if running via `run_in_background`).

3. **Collect data** — run Steps 5-6 as normal (verify repo state, fetch audit report, fetch
   commits and CI runs). The repo likely has partial results that are still valuable.

4. **Write the report** (Step 7) with status "Failed (aborted manually)" and document:
   - How far the worker got (commits pushed, CI attempts)
   - Why the test was aborted
   - Any findings from partial results

5. **Cleanup** (Step 8) as normal unless the user says `--no-cleanup`.
