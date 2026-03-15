---
name: escort
description: >
  Accompany a real user through the full pipeline — from project/feature request to deploy.
  Unlike e2e tests, this skill does NOT fail fast. The goal is to ensure the user gets their
  result (project or feature), no matter what breaks along the way. Produces a detailed report
  with all issues, warnings, and recommendations. Use when the user says "escort", "accompany",
  "babysit", "monitor user", "watch the pipeline", "сопровождение", or wants to observe a real
  user's task going through the system end-to-end and intervene if needed.
---

# Escort: User Accompaniment Through the Pipeline

You are accompanying a real user's request through the entire orchestrator pipeline.
Your job is to make sure they get their result — a deployed project or a working feature —
and to document everything that happens along the way, good and bad.

**Key difference from e2e tests**: You do NOT fail fast. If something breaks, you fix it,
work around it, or escalate it. The user must get their deliverable. The report comes second.

## Architecture Quick Reference

Understanding the pipeline flow helps you know where to look when things stall:

```
User → Telegram Bot → PO Agent (langgraph container)
  → creates Project + Story
  → publishes to scaffold:queue

scaffold:queue → Scaffolder container
  → copier template + make setup + git push
  → project status: draft → active

architect:queue → Architect container (separate from langgraph!)
  → LLM decomposes story into tasks
  → appends CI check task
  → story should transition to in_progress

Task Dispatcher (scheduler container, 30s cycle)
  → finds todo tasks with no blockers
  → publishes to engineering:queue
  → spawns worker containers via worker-manager

engineering:queue → Engineering Worker (langgraph container, separate entrypoint)
  → sends task to worker container on story/{story_id} branch
  → worker runs Claude CLI agent
  → agent commits, pushes to feature branch
  → worker-wrapper archives TASK.md+REPORT.md into .story/old_tasks/
  → worker-wrapper collects REPORT.md as task event

All tasks done → Dispatcher creates PR story/{id} → main
  → enables auto-merge (merge commit)
  → story transitions to pr_review
  → worker container cleaned up

PR CI gate:
  → CI runs on PR
  → Green CI → auto-merge → webhook (pull_request merged) → deploy:queue
  → Red CI → webhook (workflow_run failure on story/*) → creates fix task
    → story back to in_progress → fix cycle

deploy:queue → Deploy Worker (langgraph container, separate entrypoint)
  → configures GitHub secrets
  → triggers deploy.yml workflow
  → waits for workflow completion
  → runs smoke test
  → story transitions to completed
```

**Key containers** (each is separate in docker-compose):
- `langgraph` — PO agent
- `architect` — story decomposition (NOT inside scheduler!)
- `scheduler` — task dispatcher, server sync, health checks
- `engineering-worker` — engineering consumer
- `deploy-worker` — deploy consumer
- `worker-manager` — spawns worker containers
- `scaffolder` — project scaffolding

## Step 0: Discovery

Find what the user recently submitted. Look for stories created in the last 10 minutes,
then widen to 30 min and 60 min if nothing found.

```bash
# Get recent stories (newest first)
curl -s http://localhost:8000/api/stories/?sort=-created_at | python3 -c "
import json, sys
from datetime import datetime, timedelta, timezone
stories = json.load(sys.stdin)
for window in [10, 30, 60]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window)
    recent = [s for s in stories if datetime.fromisoformat(s['created_at'].replace('Z','+00:00')) > cutoff]
    if recent:
        print(f'=== Stories in last {window} minutes ===')
        for s in recent:
            print(f\"{s['id']}  {s['status']:20s}  {s['title']}  (project: {s['project_id']})\")
        print(f'Total: {len(recent)}')
        break
else:
    print('No recent stories found in the last 60 minutes')
"
```

**Filtering out test stories**: Live tests and infra tests create stories with generic titles
like "Live test story", "Infra failure story", and projects named `live-test-*`. Real user
stories have descriptive titles and meaningful project names. When multiple stories appear,
prefer the one that looks like a real user request.

**If multiple stories found**:
- Pick one to escort (prefer `created` or `in_progress` over `completed`)
- Note the others — you'll check for interference later (shared workers, queue contention,
  deploy port conflicts)

**If no stories found**:
- Increase lookback: try 30 min, then 60 min
- If still nothing: tell the user nobody submitted recently, ask if they want to wait

Save these variables for the rest of the session:
```
STORY_ID=<selected story>
PROJECT_ID=<from story response>
PROJECT_NAME=<from project.name — this is used in container names, repo names, etc.>
```

Get full context:
```bash
# Story details
curl -s http://localhost:8000/api/stories/$STORY_ID | python3 -m json.tool

# Project details (modules, config, secrets, owner)
curl -s http://localhost:8000/api/projects/$PROJECT_ID | python3 -m json.tool
```

## Step 1: Understand the Request

Read the story and project to understand what the user asked for:
- What kind of project/feature? (backend, tg_bot, frontend, combo)
- What modules are involved? (check `project.config.modules`)
- Is this a `create` (new project, status=draft) or `feature`/`fix` (existing, status=active)?
- Are there secrets configured? (check `project.config.secrets` — keys are visible, values encrypted)
- What env_hints exist? (check `project.config.env_hints` for human-readable secret descriptions)

This context helps you anticipate what might go wrong and what to watch for.

## Step 1.5: Queue Health Check

**Before waiting for the architect**, check the architect queue. Stale messages from previous
test runs or failed stories can clog the queue for hours — each message triggers a full LLM
call or a 5-minute scaffold timeout.

Cross-check both the API and raw Redis to catch desync between them.

```bash
# === Source 1: Debug API (preferred — structured, parsed) ===
# Queue health overview (all queues at once)
curl -s http://localhost:8000/debug/queues | python3 -m json.tool

# Architect queue messages (parsed, with timestamps)
curl -s "http://localhost:8000/debug/queues/architect:queue/messages?count=50" | python3 -c "
import json, sys
data = json.load(sys.stdin)
print(f'Total messages: {data[\"total\"]}')
for m in data['messages']:
    story_id = m['data'].get('story_id', '?')
    marker = ' *** OUR MESSAGE' if '$STORY_ID' in str(story_id) else ''
    print(f\"  {m['id']}  story={story_id}  ts={m['timestamp']}{marker}\")
"

# Pending messages (being processed right now)
curl -s "http://localhost:8000/debug/queues/architect:queue/architect-consumers/pending" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for p in data['pending']:
    idle_sec = p['idle_ms'] / 1000
    print(f\"  Processing: {p['id']}, idle: {idle_sec:.0f}s, deliveries: {p['delivery_count']}\")
"

# === Source 2: Raw Redis (cross-check — catches API bugs) ===
docker compose exec -T api python3 -c "
import asyncio, redis.asyncio as redis
async def check():
    r = redis.from_url('redis://redis:6379')
    try:
        info = await r.xinfo_stream('architect:queue')
        print(f'[Redis direct] Queue length: {info[\"length\"]}')
        groups = await r.xinfo_groups('architect:queue')
        for g in groups:
            print(f'  Group: {g[\"name\"].decode()}, pending: {g[\"pending\"]}, consumers: {g[\"consumers\"]}')
    except Exception as e:
        print(f'Error: {e}')
    await r.aclose()
asyncio.run(check())
"
```

**Compare the two sources**: If API says 5 messages but Redis says 8, something is filtering
or caching incorrectly — note this as a finding.

**If queue length > 5**: The queue is likely clogged with stale messages. Clean via API:

```bash
# List all messages, identify stale ones
curl -s "http://localhost:8000/debug/queues/architect:queue/messages?count=200" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for m in data['messages']:
    story_id = m['data'].get('story_id', '?')
    print(f\"{m['id']}  story={story_id}\")
print(f'Total: {data[\"total\"]}')
"

# Delete a stale message (repeat for each stale message ID)
curl -X DELETE "http://localhost:8000/debug/queues/architect:queue/messages/<message_id>"

# Ack a stuck pending message
curl -X POST "http://localhost:8000/debug/queues/architect:queue/architect-consumers/ack/<message_id>"
```

Keep the currently processing message and our story's message. Delete the rest.

## Step 2: Monitor Architect Phase

The architect runs in its **own container** (not scheduler). Check its logs directly.

```bash
# Check if tasks exist for this story
curl -s "http://localhost:8000/api/tasks/?story_id=$STORY_ID&sort=-created_at" | python3 -m json.tool
```

**What to watch**:
- Tasks created with sensible descriptions and correct blocking order
- Task count is reasonable (1-3 tasks typically, plus 1 CI check task added by system)
- No tasks with contradictory requirements

**If architect hasn't run after 2 minutes**: Check architect container logs:
```bash
# Architect has its own container — NOT scheduler!
docker compose logs architect --tail=50 --since=5m 2>/dev/null | tail -30
```

**Known architect issues**:
- **`blocked_by_task_id="None"` string bug**: The architect LLM sometimes returns the Python
  string `"None"` instead of `null` for `blocked_by_task_id`. This causes a 500 FK violation
  error. Check for this in the API logs:
  ```bash
  docker compose logs api --tail=200 --since=5m 2>/dev/null | grep -A5 "500\|ForeignKey"
  ```
  If this happens: some tasks may have been created before the error. Create the missing
  tasks manually (see Intervention section). Don't forget the CI check task.

- **Scaffold timeout**: If the project is still `draft` (scaffold hasn't run), the architect
  waits up to 5 minutes. The scaffolder has two modes: `full` (copier + make setup + git push)
  and `ensure` (just verify workspace exists). Check both scaffolder logs and the queue message
  to see which mode ran:
  ```bash
  docker compose logs scaffolder --tail=30 --since=5m 2>/dev/null
  # Also check what scaffold message was sent
  curl -s "http://localhost:8000/debug/queues/scaffold:queue/messages?count=10" | python3 -c "
  import json, sys
  for m in json.load(sys.stdin)['messages']:
      print(f\"{m['id']}  mode={m['data'].get('mode','?')}  project={m['data'].get('project_name','?')}\")
  "
  ```

**After architect completes**: Verify the task chain looks correct and the CI check task
exists. The architect's `append_ci_check_task` creates a final "Run tests, verify CI green"
task blocked by the last architect task.

## Step 3: Monitor Engineering Phase

This is the longest phase. For each task, track its journey through statuses.

```bash
# Poll task statuses (run periodically, every 30-60s)
curl -s "http://localhost:8000/api/tasks/?story_id=$STORY_ID&sort=created_at" | python3 -c "
import json, sys
tasks = json.load(sys.stdin)
for t in tasks:
    blocked = f' (blocked by {t.get(\"blocked_by_task_id\",\"\")})' if t.get('blocked_by_task_id') else ''
    elapsed = t.get('elapsed_minutes', 0)
    print(f\"{t['id']}  {t['status']:25s}  {elapsed:5.1f}m  {t['title']}{blocked}\")
"
```

### What to check at each status:

**`todo` → `in_dev`**: Task dispatcher picked it up (runs every 30s in scheduler).
- If stuck in `todo` > 2 min and not blocked by another task: check dispatcher logs
  ```bash
  docker compose logs scheduler --tail=30 --since=5m 2>/dev/null | grep -v "HTTP Request" | tail -15
  ```

**`in_dev`**: Worker is running.
- Worker container naming: `worker-{worker_id}` (where worker_id is typically the task short hash)
- Cross-check both sources to find the worker and its state:
  ```bash
  # === Source 1: Worker-Manager Introspection API ===
  # List all workers (structured, with status, project, task info)
  curl -s http://localhost:8000/wm-api/workers/ | python3 -c "
  import json, sys
  workers = json.load(sys.stdin)
  for w in workers:
      print(f\"{w['container_name']}  status={w['status']}  project={w.get('project_id','?')}  task={w.get('task_id','?')}\")
  "
  # Worker logs via API
  curl -s "http://localhost:8000/wm-api/workers/$WORKER_ID/logs?tail=30"
  # Worker workspace files via API
  curl -s "http://localhost:8000/wm-api/workers/$WORKER_ID/tree" | python3 -m json.tool
  # Read PROGRESS.md via API
  curl -s "http://localhost:8000/wm-api/workers/$WORKER_ID/files/PROGRESS.md"
  # Read agent prompts (CLAUDE.md + TASK.md)
  curl -s "http://localhost:8000/wm-api/workers/$WORKER_ID/prompts" | python3 -m json.tool

  # === Source 2: Docker direct (cross-check — catches proxy/API issues) ===
  docker ps --filter "label=com.codegen.type=worker" --format "{{.Names}}\t{{.Status}}\t{{.CreatedAt}}"
  docker logs <worker_container> --tail=30 2>&1
  docker exec <worker_container> cat /workspace/PROGRESS.md 2>/dev/null
  docker exec <worker_container> bash -c "cd /workspace && git log --oneline -5" 2>/dev/null
  ```
- **Compare**: If introspection API shows worker as "running" but `docker ps` shows it exited,
  there's a state desync in worker-manager — note as a finding.
- For workspace files when worker is already gone, use repo_id-based workspace API:
  ```bash
  # Workspaces persist after worker cleanup (stored on disk by repo_id)
  curl -s "http://localhost:8000/wm-api/workspaces/$REPO_ID/tree" | python3 -m json.tool
  curl -s "http://localhost:8000/wm-api/workspaces/$REPO_ID/files/REPORT.md"
  ```

**`in_ci`**: Code pushed, CI running.
- Check CI status via GitHub (must use GitHubAppClient from API container):
  ```bash
  REPO_SLUG="$PROJECT_NAME"  # project name IS the repo name (already hyphenated)
  docker compose exec -T api python3 -c "
  import asyncio
  from shared.clients.github import GitHubAppClient
  async def check():
      client = GitHubAppClient()
      run = await client.get_latest_workflow_run('project-factory-organization', '$REPO_SLUG', 'ci.yml', 'main')
      if run:
          print(f\"CI: {run['status']} / {run.get('conclusion','pending')} — {run['html_url']}\")
      else:
          print('No CI run found')
  asyncio.run(check())
  "
  ```

**`waiting_human_review`**: Worker hit a blocker.
- This is where escort shines. Read the blocker reason:
  ```bash
  curl -s http://localhost:8000/api/tasks/$TASK_ID | python3 -c "
  import json, sys
  t = json.load(sys.stdin)
  print('Status:', t['status'])
  print('Failure metadata:', json.dumps(t.get('failure_metadata'), indent=2))
  "
  # Also check task events for the block reason
  curl -s "http://localhost:8000/api/tasks/$TASK_ID/events" | python3 -m json.tool
  ```
- **Diagnose the issue**: Read worker logs, check workspace state, understand what went wrong
- **If fixable**: Fix it (see Intervention section below), then resume:
  ```bash
  curl -X POST "http://localhost:8000/api/tasks/$TASK_ID/resume" \
    -H "Content-Type: application/json" \
    -d '{"admin_note": "Fixed: <description of what you did>"}'
  ```
- Document everything in the report

**`done`**: Task completed successfully.
- Worker report is automatically saved as a task event by the orchestrator (worker-wrapper
  reads `/workspace/REPORT.md` after agent finishes, sends it through Redis, engineering
  consumer saves it as a `worker_report` event). Retrieve it:
  ```bash
  curl -s "http://localhost:8000/api/tasks/$TASK_ID/events?event_type=worker_report" | python3 -c "
  import json, sys
  events = json.load(sys.stdin)
  for e in events:
      report = e.get('details', {}).get('report', '')
      if report:
          print(report)
  "
  ```
- Check the commit via iteration_end event:
  ```bash
  curl -s "http://localhost:8000/api/tasks/$TASK_ID/events?event_type=iteration_end" | python3 -m json.tool
  ```

**`failed`**: Task failed permanently. Check for worker report too — it may contain diagnostics.
- Check why (failure_metadata, events, worker logs)
- If retriable: transition back and retry
  ```bash
  curl -X POST "http://localhost:8000/api/tasks/$TASK_ID/transition?to_status=backlog"
  # Then back to todo to re-trigger dispatch
  curl -X POST "http://localhost:8000/api/tasks/$TASK_ID/transition?to_status=todo"
  ```

### Polling loop

Poll every 30-60 seconds. Track time. Engineering typically takes 3-10 minutes per task.
Worker containers reuse the same Claude session across tasks in a story (via `--resume`),
so tasks after the first are often faster.

Timeout after 30 minutes per task — if it's still running, something is wrong. Check
the worker-wrapper subprocess timeout (default: 1800s = 30 min).

Keep a timeline log as you go (you'll need this for the report):
```
HH:MM  task-xxx  todo → in_dev (worker started)
HH:MM  task-xxx  in_dev — worker progress: "implementing models"
HH:MM  task-xxx  in_dev → in_ci (commit abc1234)
HH:MM  task-xxx  CI passed
HH:MM  task-xxx  done
```

## Step 4: Monitor PR Review & Deploy Phase

When all tasks are done, the dispatcher creates a PR from `story/{story_id}` → `main`,
enables auto-merge, and transitions the story to `pr_review`. Deploy is triggered later
by the webhook when the PR is merged (after CI passes).

**Flow**: `in_progress` → (all tasks done) → `pr_review` → (PR merged via webhook) → `deploying` → `completed`

**IMPORTANT**: The dispatcher's `complete_stories` function only checks stories in
`in_progress` status. If the story is still `created` (which happens when tasks were
created outside the normal architect→dispatcher flow, e.g. by escort), you must
transition it manually first:

```bash
# Check story status
curl -s http://localhost:8000/api/stories/$STORY_ID | python3 -c "
import json, sys
s = json.load(sys.stdin)
print(f\"Story: {s['status']}\")
"

# If still 'created' and all tasks are done — start it so dispatcher can complete it
curl -s -X POST "http://localhost:8000/api/stories/$STORY_ID/start" \
  -H "Content-Type: application/json" \
  -d '{"actor": "escort"}'
```

### What to watch during `pr_review`:
- PR created on GitHub (check repo PR list)
- CI running on the PR
- Auto-merge enabled
- If CI fails: webhook creates a fix task, story goes back to `in_progress`
- If CI passes: auto-merge → webhook fires → story → `deploying`

```bash
# Check if PR exists for the story branch
docker compose exec -T api python3 -c "
import asyncio
from shared.clients.github import GitHubAppClient
async def check():
    client = GitHubAppClient()
    # List open PRs
    import httpx
    token = await client.get_org_token('project-factory-organization')
    async with httpx.AsyncClient() as h:
        resp = await h.get(
            f'https://api.github.com/repos/project-factory-organization/$PROJECT_NAME/pulls',
            headers={'Authorization': f'token {token}', 'Accept': 'application/vnd.github+json'},
            params={'head': f'project-factory-organization:story/$STORY_ID', 'state': 'all'},
        )
        for pr in resp.json():
            print(f\"PR #{pr['number']}: {pr['state']}  merged={pr.get('merged',False)}  auto_merge={pr.get('auto_merge') is not None}\")
asyncio.run(check())
"
```

### Story API: Action-based endpoints

Stories use action-based endpoints, NOT generic PATCH for status changes:
```
POST /api/stories/{id}/start     → created → in_progress
POST /api/stories/{id}/deploy    → in_progress/pr_review → deploying
POST /api/stories/{id}/complete  → in_progress/deploying → completed
POST /api/stories/{id}/fail      → any → failed
POST /api/stories/{id}/reopen    → completed/failed → in_progress
POST /api/stories/{id}/archive   → created/completed → archived
```
All accept `{"actor": "escort"}` body. PATCH only updates metadata (title, description).

### Monitoring deploy

```bash
# Check deploy worker logs
docker compose logs deploy-worker --tail=50 --since=5m 2>/dev/null | grep -v "HTTP Request" | tail -20
```

**What to watch**:
- Deploy worker picks up the task
- GitHub secrets configured (REGISTRY_URL, REGISTRY_USER, REGISTRY_PASSWORD + project secrets)
- `deploy.yml` workflow dispatched and running
- Workflow completion (success or failure)
- Smoke test results
- Story transitioned to `completed`

**If deploy fails**: Check the server directly:
```bash
# Find deployed URL from deploy worker logs or service-deployments
curl -s http://localhost:8000/api/service-deployments/ | python3 -c "
import json, sys
for d in json.load(sys.stdin):
    if '$PROJECT_NAME' in d.get('project_name',''):
        print(json.dumps(d, indent=2))
"

# SSH to server
SERVER_IP=<from service-deployment or deploy logs>
bash infra/scripts/ssh-to-server.sh $SERVER_IP "cd /opt/services/$PROJECT_NAME/infra && docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml ps"
bash infra/scripts/ssh-to-server.sh $SERVER_IP "cd /opt/services/$PROJECT_NAME/infra && docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml logs --tail=50"
```

**Common deploy failures**:
- Container crash on startup (import errors, missing env vars)
- Port conflict with another deployment
- `.env` misconfigured (missing secrets)
- Docker image not pushed to registry (CI didn't run or failed)
- Workflow dispatch not triggering (check GitHub Actions tab)

## Step 5: Intervention

**The user must get their deliverable. This is the #1 priority.** You are authorized to do
whatever it takes to make that happen. There is no restricted list of interventions — if
something is broken and you can fix it, fix it.

### Mindset

Think of yourself as an on-call engineer with root access. The user paid for a working
product or feature. If the automation failed to deliver it, you step in and finish the job
by hand. Whether that means fixing code, restarting services, SSHing to a server and
editing configs, changing port allocations, rewriting broken migrations, force-pushing a
fix, manually triggering deploys — it's all on the table.

The only constraint: document everything you do in the report so we can fix the root cause
later.

### Common interventions you'll need:

- **Create missing tasks**: If architect failed partway through (e.g. the `blocked_by_task_id="None"`
  bug), create the remaining tasks manually via API. Don't forget the CI check task at the end:
  ```bash
  curl -s -X POST http://localhost:8000/api/tasks/ \
    -H "Content-Type: application/json" \
    -d '{
      "title": "...",
      "description": "...",
      "type": "create",
      "status": "todo",
      "story_id": "'$STORY_ID'",
      "project_id": "'$PROJECT_ID'",
      "blocked_by_task_id": "<previous_task_id or null>",
      "created_by": "escort"
    }'
  ```

- **Fix code directly**: Clone the repo or exec into the worker container.
  ```bash
  # Get a GitHub token for the repo
  docker compose exec -T api python3 -c "
  import asyncio
  from shared.clients.github import GitHubAppClient
  async def main():
      c = GitHubAppClient()
      token = await c.get_org_token('project-factory-organization')
      print(token)
  asyncio.run(main())
  "
  git clone https://x-access-token:<TOKEN>@github.com/project-factory-organization/$PROJECT_NAME /tmp/$PROJECT_NAME
  # ... fix, commit, push
  ```

- **Transition story status**: If state machine is stuck:
  ```bash
  curl -s -X POST "http://localhost:8000/api/stories/$STORY_ID/start" \
    -H "Content-Type: application/json" -d '{"actor": "escort"}'
  ```

- **Fix server issues**: SSH to production, edit `.env`, restart containers.
  ```bash
  bash infra/scripts/ssh-to-server.sh $SERVER_IP "cd /opt/services/$PROJECT_NAME/infra && docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml up -d"
  ```

- **Restart orchestrator services** (if stuck/crashed):
  ```bash
  docker compose restart scheduler architect engineering-worker deploy-worker
  ```
  Warning: restarting `engineering-worker` while a task is in_dev may disconnect the worker.

- **Clean queues**: Delete stale messages (see Step 1.5 for architect queue pattern;
  same approach works for `engineering:queue`, `deploy:queue`).

- **Anything else**: There is no exhaustive list. If the user's deliverable is blocked
  and you can unblock it — do it.

### What to document

For every intervention, record:
1. **What was broken** (exact error, log excerpt)
2. **What you did** (commands, files changed, commits)
3. **Why the automation didn't handle it** (root cause for the backlog)
4. **Whether it worked** (did it unblock the pipeline?)

This is the most valuable part of the escort — the report turns manual fixes into
permanent improvements.

## Step 6: Collect All Worker Reports

Worker reports are saved as task events automatically. Pull them all at once:

```bash
# For each task in the story
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
    if report:
        print(report)
    else:
        print('(no worker report)')
"
  echo
done
```

If a task has no `worker_report` event, note it in the escort report — the worker didn't
write REPORT.md (itself a finding: worker didn't follow instructions).

## Step 7: Check for Interference

If you found multiple stories during Discovery (Step 0), check whether they interfered:

- **Queue contention**: Did tasks from different stories compete for the same worker slot?
- **Deploy port conflicts**: Did two deploys try to use the same port?
- **Shared infrastructure**: Did one story's DB migration break another's?
- **Resource exhaustion**: CPU/memory pressure from parallel workers

```bash
# === Source 1: Worker-Manager Introspection API ===
curl -s http://localhost:8000/wm-api/workers/ | python3 -c "
import json, sys
workers = json.load(sys.stdin)
for w in workers:
    print(f\"{w['container_name']}  status={w['status']}  project={w.get('project_id','?')}  task={w.get('task_id','?')}\")
"

# === Source 2: Docker direct (cross-check) ===
docker ps --filter "label=com.codegen.type=worker" --format "{{.Names}}\t{{.Status}}"

# Compare: if API shows fewer workers than docker ps, worker-manager lost track of some.

# Check deploy port allocations
curl -s http://localhost:8000/api/service-deployments/ | python3 -c "
import json, sys
deps = json.load(sys.stdin)
for d in deps:
    print(f\"{d.get('project_name','?'):30s}  port={d.get('port','?')}  server={d.get('server_ip','?')}\")
"
```

## Step 8: Write the Escort Report

File: `docs/e2e_results/<project_name>-escort-<date>.md`
If file exists, add suffix: `-2`, `-3`, etc.

### Report Structure

```markdown
# Escort Report: <project_name> — <one-line summary>

> **Date**: YYYY-MM-DD
> **Project**: <project_name> (project_id: `...`)
> **Story**: <story_id> — "<story title>"
> **User**: owner_id <N>
> **Modules**: <module list>
> **Mode**: escort (observed + intervened | observe-only)
> **Result**: Delivered | Delivered with interventions | Not delivered
> **Duration**: <total time from discovery to completion>
> **Deployed URL**: <url if deployed>

## Timeline

| Time (UTC) | Event |
|---|---|
| HH:MM | Story discovered: "<title>" |
| HH:MM | Scaffold complete |
| HH:MM | Architect created N tasks |
| HH:MM | Task 1 started (worker spawned) |
| ... | ... |
| HH:MM | Deploy completed, smoke passed |

## Interventions

> Skip this section if no interventions were needed.

### Intervention N: <title>
- **When**: HH:MM UTC
- **What broke**: <description>
- **What I did**: <fix description>
- **Impact**: <did this unblock the pipeline?>

## Worker Reports

> Include the worker REPORT.md for each task. If multiple tasks, use sub-headings.

### Task 1: <task title>
<paste worker report contents>

### Task 2: <task title>
<paste worker report contents>

## Problems Found

### Problem N: <title>
- **Type**: orchestrator | template | infra | code | meta
- **Severity**: critical | major | minor | warning | recommendation
- **Status**: fixed-during-escort | needs-fix | known-issue
- **Backlog**: `#XX` | `new` | `—`
- **Description**: What happened
- **Root cause**: Why it happened (if known)
- **Evidence**: Exact error messages, log excerpts, diagnostic output
- **Suggested fix**: How to prevent this in the future

> Include EVERYTHING — not just bugs. Minor warnings, slow operations, confusing
> UX, logs that could be better, timeouts that are too aggressive, missing error
> messages — all of it goes here. The goal is a complete quality picture.

## Interference Analysis

> Skip if only one story was running.

- Other stories active during escort: <list>
- Interference detected: yes | no
- Details: <what happened>

## Metrics

- **Tasks**: N created, N completed, N failed
- **Engineering time**: Xm per task (average)
- **CI cycles**: N per task (average)
- **Deploy attempts**: N
- **Manual interventions**: N
- **Worker reports collected**: N/N

## Recommendations

Priority-ordered list of improvements discovered during this escort.

1. **[SEVERITY]** <recommendation> — <why>
2. ...
```

## Step 9: Commit the Report

```bash
git add docs/e2e_results/<project_name>-escort-<date>.md
git commit -m "escort: <project_name> — <delivered/not delivered>"
```

Do NOT push unless the user asks.

## Step 10: Skill Feedback

Write feedback to `docs/skill-feedback.md` or confirm "Skill feedback: none."

---

## Quick Reference: Key Commands

```bash
# Stories
curl -s http://localhost:8000/api/stories/?sort=-created_at
curl -s http://localhost:8000/api/stories/$STORY_ID

# Story transitions (action-based, NOT PATCH)
curl -X POST "http://localhost:8000/api/stories/$STORY_ID/start" -H "Content-Type: application/json" -d '{"actor": "escort"}'
curl -X POST "http://localhost:8000/api/stories/$STORY_ID/complete" -H "Content-Type: application/json" -d '{"actor": "escort"}'
curl -X POST "http://localhost:8000/api/stories/$STORY_ID/deploy" -H "Content-Type: application/json" -d '{"actor": "escort"}'
curl -X POST "http://localhost:8000/api/stories/$STORY_ID/fail" -H "Content-Type: application/json" -d '{"actor": "escort"}'
curl -X POST "http://localhost:8000/api/stories/$STORY_ID/reopen" -H "Content-Type: application/json" -d '{"actor": "escort"}'

# Tasks
curl -s "http://localhost:8000/api/tasks/?story_id=$STORY_ID&sort=created_at"
curl -s http://localhost:8000/api/tasks/$TASK_ID
curl -s "http://localhost:8000/api/tasks/$TASK_ID/events"
curl -s "http://localhost:8000/api/tasks/$TASK_ID/events?event_type=worker_report"
curl -s "http://localhost:8000/api/tasks/$TASK_ID/events?event_type=iteration_end"

# Task transitions
curl -X POST "http://localhost:8000/api/tasks/$TASK_ID/transition?to_status=<status>"
curl -X POST "http://localhost:8000/api/tasks/$TASK_ID/resume" -H "Content-Type: application/json" -d '{"admin_note": "..."}'

# Create task manually
curl -s -X POST http://localhost:8000/api/tasks/ -H "Content-Type: application/json" -d '{
  "title": "...", "description": "...", "type": "create", "status": "todo",
  "story_id": "...", "project_id": "...", "blocked_by_task_id": null, "created_by": "escort"
}'

# Worker containers (naming: worker-{worker_id})
# --- Via Introspection API (preferred) ---
curl -s http://localhost:8000/wm-api/workers/                              # list all workers
curl -s http://localhost:8000/wm-api/workers/$WORKER_ID/logs?tail=100      # worker logs
curl -s http://localhost:8000/wm-api/workers/$WORKER_ID/tree               # workspace file tree
curl -s http://localhost:8000/wm-api/workers/$WORKER_ID/files/PROGRESS.md  # read workspace file
curl -s http://localhost:8000/wm-api/workers/$WORKER_ID/files/REPORT.md    # read workspace file
curl -s http://localhost:8000/wm-api/workers/$WORKER_ID/prompts            # CLAUDE.md + TASK.md
curl -s http://localhost:8000/wm-api/workspaces/$REPO_ID/tree              # workspace after worker cleanup
curl -s http://localhost:8000/wm-api/workspaces/$REPO_ID/files/REPORT.md   # files after worker cleanup
# --- Via Docker direct (cross-check) ---
docker ps --filter "label=com.codegen.type=worker"
docker logs <container> --tail=100
docker exec <container> cat /workspace/PROGRESS.md
docker exec <container> cat /workspace/REPORT.md
docker exec <container> bash -c "cd /workspace && git log --oneline -5"

# Queue introspection (cross-check both sources!)
# --- Via Debug API (preferred) ---
curl -s http://localhost:8000/debug/queues                                           # all queues health
curl -s "http://localhost:8000/debug/queues/architect:queue/messages?count=50"        # queue messages
curl -s "http://localhost:8000/debug/queues/architect:queue/architect-consumers/pending"  # pending
curl -X DELETE "http://localhost:8000/debug/queues/architect:queue/messages/<msg_id>" # delete stale
curl -X POST "http://localhost:8000/debug/queues/architect:queue/architect-consumers/ack/<msg_id>"  # ack stuck
# --- Via raw Redis (cross-check) ---
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

# Service logs (each service is its own container!)
docker compose logs architect --tail=50 --since=5m      # story decomposition
docker compose logs scheduler --tail=50 --since=5m      # task dispatcher
docker compose logs engineering-worker --tail=50 --since=5m  # engineering consumer
docker compose logs deploy-worker --tail=50 --since=5m  # deploy consumer
docker compose logs worker-manager --tail=50 --since=5m # container lifecycle
docker compose logs scaffolder --tail=50 --since=5m     # project scaffolding
docker compose logs langgraph --tail=50 --since=5m      # PO agent

# Filter out HTTP noise from logs
docker compose logs <service> --tail=50 --since=5m 2>/dev/null | grep -v "HTTP Request" | tail -20

# GitHub (MUST use GitHubAppClient from API container — local gh has no access)
docker compose exec -T api python3 -c "
import asyncio
from shared.clients.github import GitHubAppClient
async def main():
    c = GitHubAppClient()
    # Get CI status:
    run = await c.get_latest_workflow_run('project-factory-organization', 'REPO', 'ci.yml', 'main')
    # Get org token for git clone:
    # token = await c.get_org_token('project-factory-organization')
asyncio.run(main())
"

# Langfuse tracing (useful for debugging architect/engineering LLM behavior)
# Traces are enriched with user_id, project_id, agent metadata.
# Access via admin UI: Tracing page, or direct Langfuse UI.
# Filter by project_id or story_id tags to find relevant traces.

# Server SSH
bash infra/scripts/ssh-to-server.sh $SERVER_IP "<command>"

# Service deployments
curl -s http://localhost:8000/api/service-deployments/ | python3 -m json.tool
```

## Common Gotchas

1. **Architect is its own container** — `docker compose logs architect`, NOT `docker compose logs scheduler`
2. **Repo names use hyphens, not underscores**: `todo_api` → `todo-api` in GitHub. Project name = repo name.
3. **Local `gh` CLI has no access** to `project-factory-organization` — always use `GitHubAppClient` via `docker compose exec -T api`
4. **Import path**: `from shared.clients.github import GitHubAppClient` (NOT `shared.github_app`)
5. **Worker containers are named** `worker-{worker_id}` — use introspection API (`/wm-api/workers/`) or `docker ps` with label filter
6. **Story must be `in_progress` for PR creation** — if story is stuck in `created` after all tasks done, `POST .../start` it manually. After PR is created, story goes to `pr_review`. Deploy triggers only after PR is merged (webhook)
7. **Story transitions are action-based** — use `POST /start`, `/complete`, `/deploy`, etc. Not `PATCH` with status field.
8. **Stale queue messages** from test runs can clog architect for hours — always check queue health early (use `/debug/queues` API)
9. **Don't restart services carelessly** — if you restart `engineering-worker`, active tasks may lose their worker connection
10. **Project model has no `created_at` field** — filter by story `created_at` instead when looking for recent activity
11. **Scaffold has ensure-workspace gate** — scaffolder may run in `mode: "ensure"` (just verify workspace exists) vs `mode: "full"` (copier + setup + push). Check scaffold logs for which mode ran.
12. **Cross-check sources** — always compare API data with Docker/Redis direct access. Desync between them is itself a finding worth reporting.
13. **Langfuse for LLM debugging** — architect and engineering traces are in Langfuse, tagged with project_id. Use admin UI Tracing page or direct Langfuse to inspect what the LLM did.
