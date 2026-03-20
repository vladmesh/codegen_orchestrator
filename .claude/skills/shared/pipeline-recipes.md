# Pipeline Recipes — Shared Reference

Common bash recipes used by e2e-run, escort, and other pipeline-monitoring skills.
Read this file when referenced by a skill — don't memorize it.

---

## Queue Health Check

Cross-check Debug API and raw Redis. Stale messages can clog architect for hours.

```bash
# Debug API — all queues at once
curl -s http://localhost:8000/debug/queues | python3 -m json.tool

# Architect queue messages (parsed, with timestamps)
curl -s "http://localhost:8000/debug/queues/architect:queue/messages?count=50" | python3 -c "
import json, sys
data = json.load(sys.stdin)
print(f'Total messages: {data[\"total\"]}')
for m in data['messages']:
    story_id = m['data'].get('story_id', '?')
    print(f\"  {m['id']}  story={story_id}  ts={m['timestamp']}\")
"

# Pending messages (being processed right now)
curl -s "http://localhost:8000/debug/queues/architect:queue/architect-consumers/pending" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for p in data['pending']:
    idle_sec = p['idle_ms'] / 1000
    print(f\"  Processing: {p['id']}, idle: {idle_sec:.0f}s, deliveries: {p['delivery_count']}\")
"

# Raw Redis cross-check (catches API bugs)
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

**If queue length > 5** — clean stale messages:

```bash
# List all messages
curl -s "http://localhost:8000/debug/queues/architect:queue/messages?count=200" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for m in data['messages']:
    story_id = m['data'].get('story_id', '?')
    print(f\"{m['id']}  story={story_id}\")
print(f'Total: {data[\"total\"]}')
"

# Delete stale message
curl -X DELETE "http://localhost:8000/debug/queues/architect:queue/messages/<message_id>"

# Ack stuck pending message
curl -X POST "http://localhost:8000/debug/queues/architect:queue/architect-consumers/ack/<message_id>"
```

---

## Task Status Polling

```bash
curl -s "http://localhost:8000/api/tasks/?story_id=$STORY_ID&sort=created_at" | python3 -c "
import json, sys
tasks = json.load(sys.stdin)
for t in tasks:
    blocked = f' (blocked by {t.get(\"blocked_by_task_id\",\"\")})' if t.get('blocked_by_task_id') else ''
    print(f\"{t['id']}  {t['status']:25s}  {t['title']}{blocked}\")
"
```

---

## Worker Monitoring

### Via Docker (most reliable)

```bash
docker ps --filter "label=com.codegen.type=worker" --format "{{.Names}}\t{{.Status}}"

# Worker logs
WORKER_CONTAINER=$(docker ps --filter "label=com.codegen.type=worker" --format "{{.Names}}" | head -1)
docker logs "$WORKER_CONTAINER" --tail=20 2>&1
```

### Via Worker-Manager Introspection API

```bash
# List all workers
curl -s http://localhost:8000/wm-api/workers/ | python3 -c "
import json, sys
data = json.load(sys.stdin)
if isinstance(data, list):
    for w in data:
        print(f\"{w['container_name']}  status={w['status']}  task={w.get('task_id','?')}\")
else:
    print(f'WM API error: {data}')
" 2>/dev/null || echo "WM API unavailable"

# Worker logs, workspace, prompts via API
curl -s "http://localhost:8000/wm-api/workers/$WORKER_ID/logs?tail=30"
curl -s "http://localhost:8000/wm-api/workers/$WORKER_ID/tree" | python3 -m json.tool
curl -s "http://localhost:8000/wm-api/workers/$WORKER_ID/files/PROGRESS.md"
curl -s "http://localhost:8000/wm-api/workers/$WORKER_ID/files/REPORT.md"
curl -s "http://localhost:8000/wm-api/workers/$WORKER_ID/prompts" | python3 -m json.tool
```

### Workspace after worker cleanup

```bash
curl -s "http://localhost:8000/wm-api/workspaces/$REPO_ID/tree" | python3 -m json.tool
curl -s "http://localhost:8000/wm-api/workspaces/$REPO_ID/files/REPORT.md"
```

---

## CI Status Check

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

---

## GitHub Access

**Local `gh` CLI has NO access to `project-factory-organization`.** Always use `GitHubAppClient` via docker compose exec:

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
    # gh.get_org_token(org) -> str
    result = await gh.list_repo_files('project-factory-organization', 'REPO_NAME')
    print(result)
asyncio.run(main())
"
```

---

## Server Access

```bash
bash infra/scripts/ssh-to-server.sh $SERVER_IP "<command>"
```

**Server filesystem**: `/opt/services/<REPO_NAME>/` (may use hyphens OR underscores — check actual repo name from API)

```bash
# Docker compose on server — always use both compose files:
COMPOSE="docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml"
$COMPOSE ps -a
$COMPOSE logs backend --tail=50
```

---

## Story API — Action-Based Transitions

Stories use action-based endpoints, NOT generic PATCH:
```
POST /api/stories/{id}/start     → created → in_progress
POST /api/stories/{id}/deploy    → in_progress/pr_review → deploying
POST /api/stories/{id}/complete  → in_progress/deploying → completed
POST /api/stories/{id}/fail      → any → failed
POST /api/stories/{id}/reopen    → completed/failed → in_progress
POST /api/stories/{id}/archive   → created/completed → archived
```
All accept `{"actor": "<caller>"}` body. PATCH only updates metadata (title, description).

---

## Worker Report Collection

```bash
mkdir -p docs/e2e_results/worker_reports
DATE=$(date +%Y%m%d)
WORKER_REPORT="docs/e2e_results/worker_reports/${PROJECT_NAME}-${DATE}-worker.md"

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
    TITLE=$(curl -s "http://localhost:8000/api/tasks/$TASK_ID" | python3 -c "import json,sys; print(json.load(sys.stdin)['title'])")
    echo "## Task: $TASK_ID — $TITLE" >> "$WORKER_REPORT"
    echo "" >> "$WORKER_REPORT"
    echo "$REPORT" >> "$WORKER_REPORT"
    echo "" >> "$WORKER_REPORT"
    FOUND_REPORTS=$((FOUND_REPORTS + 1))
  fi
done
```

**Fallback — workspace archive** (if API returned nothing):

```bash
if [ "$FOUND_REPORTS" -eq 0 ] && [ -n "$REPO_ID" ]; then
  # Read from scaffolder workspace
  ARCHIVE_FILES=$(docker compose exec -T scaffolder sh -c \
    "ls /data/workspaces/$REPO_ID/.story/old_tasks/*.md 2>/dev/null" || true)
  if [ -n "$ARCHIVE_FILES" ]; then
    for ARCHIVE in $ARCHIVE_FILES; do
      CONTENT=$(docker compose exec -T scaffolder cat "$ARCHIVE" 2>/dev/null)
      if [ -n "$CONTENT" ]; then
        echo "## Archive: $(basename $ARCHIVE)" >> "$WORKER_REPORT"
        echo "" >> "$WORKER_REPORT"
        echo "$CONTENT" >> "$WORKER_REPORT"
        echo "" >> "$WORKER_REPORT"
        FOUND_REPORTS=$((FOUND_REPORTS + 1))
      fi
    done
  fi
  # Host volume fallback
  if [ "$FOUND_REPORTS" -eq 0 ]; then
    docker run --rm -v /data/workspaces:/workspaces alpine sh -c \
      "cat /workspaces/$REPO_ID/.story/old_tasks/*.md 2>/dev/null" \
      >> "$WORKER_REPORT" 2>/dev/null && \
      FOUND_REPORTS=$((FOUND_REPORTS + 1)) || true
  fi
fi
```

---

## Service Logs (filter HTTP noise)

```bash
docker compose logs <service> --tail=50 --since=5m 2>/dev/null | grep -v "HTTP Request" | tail -20
```

Key service names: `architect`, `scheduler`, `engineering-worker`, `deploy-worker`, `qa-worker`, `worker-manager`, `scaffolder`, `langgraph`

---

## Common Gotchas

1. **Architect is its own container** — `docker compose logs architect`, NOT scheduler
2. **Repo names may use hyphens OR underscores**: PO decides. Always check both variants
3. **Local `gh` CLI has no access** — always use `GitHubAppClient` via docker compose exec
4. **Import path**: `from shared.clients.github import GitHubAppClient`
5. **Worker containers**: use `docker ps --filter "label=com.codegen.type=worker"` (primary). WM API may return 404 or non-list — handle errors
6. **Story must be `in_progress` for PR creation** — nudge with `POST .../start` if stuck
7. **Story transitions are action-based** — `POST /start`, `/complete`, NOT PATCH
8. **Stale queue messages** can clog architect for hours — check queues early
9. **Project needs a Repository record** — scaffold_trigger won't fire without it
10. **Cross-check sources** — compare API data with Docker/Redis. Desync is a finding
11. **Deploy is triggered by `poll_merged_prs()` poller** (every 30s in scheduler) — do NOT manually trigger deploys, the poller handles it
12. **Deploy Run record uses `type` field** (not `run_type`)
13. **DeployMessage requires `task_id`** — this is actually the Run ID, not a task ID
14. **QA phase** — story goes `deploying` → `testing` → `completed`. qa-worker SSHes to prod, runs Claude Code CLI
