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

## Key References
- [docs/PIPELINE_V2.md](docs/PIPELINE_V2.md) — full pipeline architecture and status transitions
- [docs/parallel-workers.md](docs/parallel-workers.md) — worker containers, networks, bind-mounts
- [docs/DEPLOY.md](docs/DEPLOY.md) — deploy workflow, GitHub Actions, server setup
- [docs/SECRETS.md](docs/SECRETS.md) — secret levels and handling
- [docs/resource-management.md](docs/resource-management.md) — handles vs secrets, ResourceAllocator

**Key containers** (each is separate in docker-compose):
- `langgraph` — PO agent
- `architect` — story decomposition (NOT inside scheduler!)
- `scheduler` — task dispatcher, server sync, health checks
- `engineering-worker` — engineering consumer
- `deploy-worker` — deploy consumer
- `qa-worker` — QA consumer (post-deploy testing via Claude Code on prod server)
- `worker-manager` — spawns worker containers
- `scaffolder` — project scaffolding

**Status flow**: `draft → scaffold → active → architect → tasks (todo→in_dev→done) → pr_review → deploying → testing → completed`

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

**Before waiting for the architect**, check the architect queue for stale messages.

> **Recipes**: See "Queue Health Check" in `.claude/skills/shared/pipeline-recipes.md` for all bash commands (Debug API, raw Redis cross-check, stale message cleanup).

**Compare the two sources**: If API and Redis disagree on message count — note as a finding.
Keep the currently processing message and our story's message. Delete the rest.

## Step 2: Monitor Architect Phase

The architect runs in its **own container** (not scheduler). Check its logs directly.

```bash
# Check if tasks exist for this story
curl -s "http://localhost:8000/api/tasks/?story_id=$STORY_ID&sort=-created_at" | python3 -m json.tool
```

**What to watch**:
- Tasks created with sensible descriptions and correct blocking order
- Task count is reasonable (1-3 tasks typically)
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
  tasks manually (see Intervention section).

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

**After architect completes**: Verify the task chain looks correct — sensible descriptions,
proper blocking order, no contradictions.

## Step 3: Monitor Engineering Phase

This is the longest phase. For each task, track its journey through statuses.

> **Recipes**: See "Task Status Polling", "Worker Monitoring", and "CI Status Check" in `.claude/skills/shared/pipeline-recipes.md` for bash commands.

### What to check at each status:

**`todo` → `in_dev`**: Task dispatcher picked it up (runs every 30s in scheduler).
- If stuck in `todo` > 2 min and not blocked by another task: check dispatcher logs
  ```bash
  docker compose logs scheduler --tail=30 --since=5m 2>/dev/null | grep -v "HTTP Request" | tail -15
  ```

**`in_dev`**: Worker is running. Use Worker Monitoring recipes (Docker ps + WM API) from pipeline-recipes.md.
- Cross-check both sources. If API shows "running" but `docker ps` shows exited — state desync, note as finding.
- For workspace files after worker cleanup: use `wm-api/workspaces/$REPO_ID/` endpoints.

**`in_ci`**: Code pushed, CI running. Use CI Status Check recipe from pipeline-recipes.md.

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
enables auto-merge, and transitions the story to `pr_review`. Deploy is triggered by
`poll_merged_prs()` in the scheduler (runs every 30s) — it detects merged PRs for
stories in `pr_review` and publishes a deploy message.

**Flow**: `in_progress` → (all tasks done) → `pr_review` → (PR merged, poller detects) → `deploying` → `testing` → `completed`

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
- If CI fails: `poll_ci_failures()` creates a fix task, story goes back to `in_progress`
- If CI passes: auto-merge → `poll_merged_prs()` detects merge → story → `deploying`

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

> See "Story API — Action-Based Transitions" in `.claude/skills/shared/pipeline-recipes.md`.
> All accept `{"actor": "escort"}` body. PATCH only updates metadata (title, description).

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

### Monitoring QA phase

After deploy succeeds, the deploy-worker publishes a `QAMessage` to `qa:queue` and
transitions the story to `testing`. The `qa-worker` container SSHes to the prod server
and runs Claude Code CLI to test the deployed project as a real user.

```bash
# QA worker logs
docker compose logs qa-worker --tail=50 --since=5m 2>/dev/null | grep -v "HTTP Request" | tail -20

# Check qa:queue for messages
curl -s "http://localhost:8000/debug/queues/qa:queue/messages?count=10" | python3 -m json.tool

# Check pending (being processed)
curl -s "http://localhost:8000/debug/queues/qa:queue/qa-consumers/pending" | python3 -m json.tool
```

**What to watch**:
- QA consumer picks up the message
- SSH connection to prod server succeeds
- Claude Code runs QA prompt (tests endpoints, checks responses against story)
- QA result parsed (JSON with `pass`, `checks`, `summary`)
- Story: `testing` → `completed` (pass) or back to `in_progress` (fail, creates fix task)

**QA timeout**: 20 minutes. Poll story status every 30s.

**Common QA failures**:
- SSH connection failed (server unreachable, credentials expired)
- Claude Code not installed on server (run `qa_runner` Ansible role)
- Claude Code session expired (re-copy `.credentials.json` from orchestrator host)
- QA prompt produced unparseable output (non-JSON response from Claude)
- Server swap not configured (Claude Code needs ~2GB to run, OOM without swap)

**If QA is stuck**: Check if the message was consumed and if there's a pending entry.
If the qa-worker crashed, restart it:
```bash
docker compose restart qa-worker
```

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
  bug), create the remaining tasks manually via API:
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

> See "Worker Report Collection" in `.claude/skills/shared/pipeline-recipes.md` for the full bash script.

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

## Quick Reference

> All bash recipes (queue health, worker monitoring, CI checks, GitHub/server access, story transitions, service logs) are in `.claude/skills/shared/pipeline-recipes.md`. Read it when you need a specific command.

**Escort-specific commands not in shared recipes:**

```bash
# Task transitions
curl -X POST "http://localhost:8000/api/tasks/$TASK_ID/transition?to_status=<status>"
curl -X POST "http://localhost:8000/api/tasks/$TASK_ID/resume" -H "Content-Type: application/json" -d '{"admin_note": "..."}'

# Create task manually
curl -s -X POST http://localhost:8000/api/tasks/ -H "Content-Type: application/json" -d '{
  "title": "...", "description": "...", "type": "create", "status": "todo",
  "story_id": "...", "project_id": "...", "blocked_by_task_id": null, "created_by": "escort"
}'

# Langfuse tracing (architect/engineering LLM debugging)
# Filter by project_id or story_id tags in admin UI Tracing page
```

## Common Gotchas

> See "Common Gotchas" in `.claude/skills/shared/pipeline-recipes.md` for the full list.

**Escort-specific additions:**
- **Don't restart services carelessly** — restarting `engineering-worker` disconnects active tasks
- **Project model has no `created_at`** — filter by story `created_at` when finding recent activity
- **Scaffold modes**: `full` (copier + setup + push) vs `ensure` (just verify workspace). Check scaffold logs.
- **Langfuse for LLM debugging** — architect and engineering traces tagged with project_id
- **QA timeout is 20 minutes** — if story stays in `testing` longer, check qa-worker logs
