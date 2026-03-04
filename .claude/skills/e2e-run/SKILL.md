---
name: e2e-run
description: Run Line 2 E2E test — submit engineering task, wait for completion, verify, write report. Use when user wants to test the engineering pipeline end-to-end.
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
need a `TELEGRAM_BOT_TOKEN` for **Level C** (deploy). Level A/B work without it.

Secrets are read from `.claude/e2e-secrets.env` (gitignored). If missing, tell the user:

```
Create .claude/e2e-secrets.env (see .claude/e2e-secrets.env.example).
A test bot token from @BotFather is needed for Level C tg_bot tests.
```

**Injection**: After creating the project in Step 1, if the test includes `tg_bot` AND level is C,
inject secrets into `project.config.secrets` so the DevOps subgraph can resolve them during deploy:

```bash
# Read token from secrets file
TG_TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' .claude/e2e-secrets.env 2>/dev/null | cut -d= -f2-)

if [ -z "$TG_TOKEN" ]; then
  echo "ERROR: TELEGRAM_BOT_TOKEN not found in .claude/e2e-secrets.env"
  echo "Level C tg_bot tests require a bot token. See .claude/e2e-secrets.env.example"
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

Skip this step entirely for Level A/B or tests without `tg_bot` module.

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

## Server Access (Level C only)

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

**Finding the server IP**: The resource allocator assigns a port on a managed server.
The `service-deployments` API only has records if deploy succeeded. For failed deploys,
get the IP from the port allocations API instead:

```bash
# Option 1: from service-deployments (only if deploy succeeded)
SERVER_IP=$(curl -s "http://localhost:8000/api/service-deployments/?project_id=$PROJECT_ID" | jq -r '.[0].server_ip // empty')

# Option 2: from port allocations (always works — allocated during engineering)
SERVER_IP=$(curl -s "http://localhost:8000/api/servers/vps-267180/ports" \
  | jq -r --arg pid "$PROJECT_ID" '.[] | select(.project_id == $pid) | .server_ip // empty' )

# Option 3: if you don't know which server, check all managed servers
SERVER_IP=$(curl -s "http://localhost:8000/api/servers/?is_managed=true" \
  | jq -r '.[].handle' \
  | while read h; do
      curl -s "http://localhost:8000/api/servers/$h/ports" \
        | jq -r --arg pid "$PROJECT_ID" '.[] | select(.project_id == $pid) | .server_ip // empty'
    done | head -1)
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

### Step 0: Health check + pre-flight cleanup

Before the first test, verify the stack is healthy:

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
  | sort | xargs sha256sum 2>/dev/null | sha256sum | cut -c1-16)

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

**Pre-flight: clean up stale artifacts** (run for every test, essential for Level C):

```bash
ORG="project-factory-organization"
# Convert PROJECT_NAME to repo slug (underscores → hyphens)
REPO_SLUG=$(echo "$PROJECT_NAME" | tr '_' '-')

# 1. Check and delete leftover GitHub repo
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
" 2>/dev/null)

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

**Level C only** — also check target servers for stale deployments:

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

**If test includes `tg_bot` AND level is C** — inject secrets now (see "E2E Secrets" section above).
If secrets file is missing or token is empty, STOP this test.

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

Poll based on level. Print status updates every check.

**Worker log checks**: During polling, periodically check worker container logs to understand
what the agent is doing (especially useful for long waits):

```bash
# Check worker progress (find container name from Step 3 logs)
docker logs worker-dev-$PROJECT_NAME_SLUG-* --tail=10 2>&1
# Also check engineering-worker for subgraph-level events
docker compose logs engineering-worker --tail=10 --since=120s 2>&1 | grep -v "health"
```

**Level B CI fix tracking**: The worker may go through multiple CI fix cycles (push → CI fail →
fix → push). Track each cycle by fetching all CI runs and commits when the task completes or
when you need to understand progress:

```bash
ORG="project-factory-organization"
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
            'https://api.github.com/repos/$ORG/$PROJECT_NAME/commits?per_page=10',
            headers={'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}
        )
        print('=== Commits ===')
        for c in r.json():
            msg = c['commit']['message'].split(chr(10))[0]
            print(f'{c[\"sha\"][:8]} {c[\"commit\"][\"author\"][\"date\"]} {msg}')
        # All CI runs
        r = await http.get(
            'https://api.github.com/repos/$ORG/$PROJECT_NAME/actions/runs?per_page=10',
            headers={'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}
        )
        print('=== CI Runs ===')
        for run in r.json().get('workflow_runs', []):
            print(f'Run #{run[\"id\"]}: status={run[\"status\"]}, conclusion={run.get(\"conclusion\")}, created={run[\"created_at\"]}')

asyncio.run(main())
" 2>/dev/null
```

This data is essential for the report — include each CI fix attempt in the Timeline.

**Level A** — poll GitHub for code (don't wait for task completion):

```bash
ORG="project-factory-organization"
# Poll every 60s, timeout after 30 minutes
for i in $(seq 1 30); do
  FILES=$(docker compose exec -T langgraph python -c "
import asyncio
from shared.clients.github import GitHubAppClient
async def main():
    gh = GitHubAppClient()
    files = await gh.list_repo_files('$ORG', '$PROJECT_NAME')
    print('\n'.join(files))
asyncio.run(main())
" 2>/dev/null)
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
ORG="project-factory-organization"
docker compose exec -T langgraph python -c "
import asyncio
from shared.clients.github import GitHubAppClient

async def main():
    gh = GitHubAppClient()
    # List root files
    files = await gh.list_repo_files('$ORG', '$PROJECT_NAME')
    print('Root files:', sorted(files))
    # Check for expected files: Makefile, pyproject.toml, services/
    # Fetch latest commit via GitHub API
    token = await gh.get_org_token('$ORG')
    import httpx
    async with httpx.AsyncClient() as http:
        r = await http.get(
            'https://api.github.com/repos/$ORG/$PROJECT_NAME/commits?per_page=3',
            headers={'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}
        )
        if r.status_code == 200:
            for c in r.json():
                msg = c['commit']['message'].split(chr(10))[0]
                print(f\"  {c['sha'][:8]} {msg}\")

asyncio.run(main())
"
```

**Level B** — CI passed:

```bash
ORG="project-factory-organization"
docker compose exec -T langgraph python -c "
import asyncio
from shared.clients.github import GitHubAppClient

async def main():
    gh = GitHubAppClient()
    try:
        run = await gh.get_latest_workflow_run('$ORG', '$PROJECT_NAME', 'ci.yml', 'main')
        print(f\"CI run #{run['id']}: status={run['status']}, conclusion={run.get('conclusion')}\")
        print(f\"URL: {run['html_url']}\")
    except Exception as e:
        print(f'CI check failed: {e}')

asyncio.run(main())
"
```

**Level C** — service running on server:

First, find the server IP (see "Server Access" section). Then verify or diagnose:

```bash
# Get server IP from port allocations (works even if deploy failed)
SERVER_IP=$(curl -s "http://localhost:8000/api/servers/?is_managed=true" \
  | jq -r '.[].handle' \
  | while read h; do
      curl -s "http://localhost:8000/api/servers/$h/ports" \
        | jq -r --arg pid "$PROJECT_ID" '.[] | select(.project_id == $pid) | .server_ip // empty'
    done | head -1)
echo "Server: $SERVER_IP"
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

# Curl the health endpoint (port from allocation, default 8000)
DEPLOY_PORT=$(curl -s "http://localhost:8000/api/servers/?is_managed=true" \
  | jq -r '.[].handle' \
  | while read h; do
      curl -s "http://localhost:8000/api/servers/$h/ports" \
        | jq -r --arg pid "$PROJECT_ID" '.[] | select(.project_id == $pid) | .port // empty'
    done | head -1)
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

# Determine report filename (must match main report name + "-worker" suffix)
# Use the same naming logic as Step 7: <project_name>-<date>-level<X>[-N]-worker.md
WORKER_REPORT="docs/e2e_results/worker_reports/${PROJECT_NAME}-${DATE}-level${LEVEL}-worker.md"

# Fetch via GitHubAppClient
docker compose exec -T langgraph python -c "
import asyncio
from shared.clients.github import GitHubAppClient

async def main():
    gh = GitHubAppClient()
    content = await gh.get_file_contents('$ORG', '$PROJECT_NAME', 'AUDIT_REPORT.md')
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
append a suffix: `-2`, `-3`, etc. (e.g., `todo_api-20260302-levelC-2.md`). Each E2E run
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
> **Test level**: A / B / C
> **Status**: Passed / Failed
> **Worker audit**: collected (findings included below) | not found

---

## Timeline
(chronological log of what happened — key timestamps and events)

## Problems Found

### Problem 1: <title>
- **Type**: orchestrator | template | meta | other
- **Severity**: critical / major / minor
- **Backlog**: `#XX` (existing) | `new` (for /triage to create) | `—` (skip)
- **Description**: ...
- **Root cause**: ...
- **Suggested fix**: ...
```

### Step 7.5: Commit reports

```bash
git add docs/e2e_results/<project_name>-<date>.md
git add docs/e2e_results/worker_reports/ 2>/dev/null || true
git commit -m "e2e: <project_name> level <X> — <pass/fail>"
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
    await gh.delete_repo('project-factory-organization', '$PROJECT_NAME')
    print('Repo deleted')

asyncio.run(main())
"
```

**Level C only** — also clean server deployment:

```bash
# 3. Get server IP — try service-deployments first, fall back to port allocations
DEPLOYMENTS=$(curl -s "http://localhost:8000/api/service-deployments/?project_id=$PROJECT_ID")
SERVER_IP=$(echo "$DEPLOYMENTS" | jq -r '.[0].server_ip // empty')

# Fallback: port allocations (works even if deploy failed and has no deployment records)
if [ -z "$SERVER_IP" ]; then
  SERVER_IP=$(curl -s "http://localhost:8000/api/servers/?is_managed=true" \
    | jq -r '.[].handle' \
    | while read h; do
        curl -s "http://localhost:8000/api/servers/$h/ports" \
          | jq -r --arg pid "$PROJECT_ID" '.[] | select(.project_id == $pid) | .server_ip // empty'
      done | head -1)
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
