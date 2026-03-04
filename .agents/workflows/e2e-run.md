---
description: Run E2E test — submit engineering task, wait for completion, verify, write report. Use when user wants to test the engineering pipeline end-to-end.
---

# E2E Engineering Test Runner

Run one or more E2E tests end-to-end: create project, trigger engineering, monitor progress, verify results (including deploy), collect audit report, write investigation report, cleanup.

## Arguments
- `test selector` (REQUIRED):
  - Project name: `todo_api`, `echo_bot`, `landing_page`, `weather_bot`, `url_shortener`, `bot_landing`, `expense_tracker`
  - Comma-separated or `all` to run all.
- `--with-po` — route through PO agent (creates test user, sends to `po:input`, PO creates project & triggers engineering).
- `--no-cleanup` — skip cleanup after test.

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

## GitHub Access
Use `GitHubAppClient` via `docker compose exec` instead of `gh` CLI. Example:
```bash
docker compose exec -T langgraph python -c "
import asyncio
from shared.clients.github import GitHubAppClient

async def main():
    gh = GitHubAppClient()
    result = await gh.list_repo_files('project-factory-organization', 'REPO_NAME')
    print(result)

asyncio.run(main())
"
```

## Execution Flow

For each selected test, sequentially execute:

### Step 0: Health check + pre-flight cleanup
1. Verify stack healthy: `curl -sf http://localhost:8000/health | jq .`
2. Worker image staleness check (compare source hash vs docker label).
3. Delete leftover GitHub repo (`REPO_SLUG=$(echo "$PROJECT_NAME" | tr '_' '-')`).
4. Kill leftover worker containers: `docker ps --filter "name=dev-" --format "{{.Names}}" | grep "$REPO_SLUG" | xargs -r docker rm -f`.
5. Check managed servers for stale `/opt/services/<PROJECT_NAME>/` deployments.

### Step 1: Create project (direct mode — default)
Skip if `--with-po`. Generate `PROJECT_ID` with `uuidgen`.
POST to `/api/projects/` with `id`, `name`, and `config` (modules + task description + audit instructions from `.claude/skills/e2e-run/SKILL.md`).
If test includes `tg_bot`, inject `TELEGRAM_BOT_TOKEN` from `.claude/e2e-secrets.env`.

### Step 2: Trigger engineering (direct mode — default)
Skip if `--with-po`. Generate `TASK_ID`. POST to `/api/tasks/`. Publish `EngineeringMessage` to `engineering:queue` with `skip_deploy=False`.

### Step 1-PO: Create test user & send to PO (--with-po mode)
Skip unless `--with-po`.
1. Upsert test user: `POST /api/users/upsert` with `telegram_id: 999000001, username: e2e_test_user`.
2. Publish `POUserMessage` to `po:input` with `user_id: "999000001"` and full project description.
3. Wait for PO response on `po:response:{request_id}` (120s timeout).
4. Extract `PROJECT_ID` and `TASK_ID` from API (`/api/projects/`, `/api/tasks/`).
5. If PO failed after 2 attempts, fall back to direct mode and note in report.
6. If test includes `tg_bot`, inject secrets.

### Step 3: Verify scaffold started
Wait 20s. Check `docker compose logs worker-manager`. If missing `scaffold` or `copier`, abort test immediately.

### Step 4: Monitor
Poll engineering task status every 30s (timeout 60min). Then find deploy task and poll every 30s (timeout 30min). Check worker container logs and CI runs periodically.

### Step 5: Verify
1. Check CI run status via `GitHubAppClient`.
2. Find server IP from port allocations. SSH to server, verify containers, check health endpoint.
3. If deploy failed, collect crash diagnostics (container logs, .env, restart counts).

### Step 6: Collect worker audit report
Fetch `AUDIT_REPORT.md` from the repo. Save to `docs/e2e_results/worker_reports/<project_name>-<date>-worker.md`.

### Step 7: Write E2E report
Write report to `docs/e2e_results/<project_name>-<date>.md`. Never overwrite existing — use `-2`, `-3` suffix. Include PO interaction section if `--with-po`. Classify problems by type: orchestrator, template, meta, or other. Commit both files.

### Step 8: Cleanup (skip if --no-cleanup)
1. Kill worker containers.
2. Delete GitHub repo via `GitHubAppClient`.
3. Remove app from server via SSH, delete deployment records.
4. Delete project from DB (`DELETE /api/projects/$PROJECT_ID`).
5. If `--with-po`: clean PO thread checkpoint (`po-user-999000001`).

## Abort & Collect
If manual interrupt requested: kill worker, stop polling, collect partial results (commits/CI), write failed report, cleanup.
