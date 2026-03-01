# Line 2: Engineering Flow Playbook

Manual test playbook: submit engineering tasks for every valid module combination,
verify that Claude Code (or Factory.ai) builds a working project end-to-end.

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

### Audit Prompt

Appended to every task description (goes into TASK.md via `config.description`):

```
## Audit Instructions

In addition to completing the task above, you are performing an audit of the
framework and development environment.

Throughout your work, keep a file called `AUDIT_REPORT.md` in the repo root.
Log everything you encounter:
- Problems, errors, or unexpected behavior
- Missing features or tools in the framework
- Anything that didn't work as expected or required workarounds
- Suggestions for improving the template, framework, or workspace setup
- Ideas for making the development flow smoother

Be specific: include exact error messages, file paths, and what you expected
vs what happened. This report is as valuable as the code itself.
```

---

## Test Levels

### Level A: Code Generation (fastest)

**What**: Engineering worker creates repo, scaffolds, spawns developer worker. Developer writes code and pushes. We verify code exists in GitHub. **Do not wait** for CI or deploy.

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

- Stack is running: `make up`
- Engineering worker consuming: check `docker compose logs engineering-worker`
- Worker-manager running: check `docker compose logs worker-manager`
- For Level C: at least one managed server with capacity in DB

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

```bash
# Level A/B: skip deploy
SKIP_DEPLOY=true

# Level C: full flow with deploy
# SKIP_DEPLOY=false

docker compose exec -T langgraph python -c "
import asyncio, json, uuid, os
os.environ.setdefault('ORCHESTRATOR_API_URL', 'http://api:8000')
os.environ.setdefault('REDIS_URL', 'redis://redis:6379')

from orchestrator_cli.commands.engineering import trigger_engineering_async

result = asyncio.run(trigger_engineering_async(
    project_id='$PROJECT_ID',
    action='create',
    skip_deploy=$SKIP_DEPLOY,
))
print(json.dumps(result, indent=2))
"
```

Save the `task_id` from output.

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
- **Cost**: Each test spawns a Claude Code (or Factory.ai) worker session. Budget accordingly.
