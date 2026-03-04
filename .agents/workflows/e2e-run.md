---
description: Run E2E test — submit engineering task, wait for completion, verify, write report. Use when user wants to test the engineering pipeline end-to-end.
---

# E2E Engineering Test Runner

Run one or more E2E tests end-to-end: create project, trigger engineering, monitor progress, verify results, collect audit report, write investigation report, cleanup.

## Arguments
- `test selector` (REQUIRED):
  - Project name: `todo_api`, `echo_bot`, `landing_page`, `weather_bot`, `url_shortener`, `bot_landing`, `expense_tracker`
  - Comma-separated or `all` to run all.
- `test level` (default: `A`):
  - `A` — Code generation only (~10-20 min). Verify code in GitHub.
  - `B` — Engineering + CI (~20-40 min). Wait for task completion + CI pass.
  - `C` — Full flow + deploy (~30-60 min). Verify service running on server.
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
2. Delete leftover GitHub repo (`REPO_SLUG=$(echo "$PROJECT_NAME" | tr '_' '-')`).
3. Kill leftover worker containers: `docker ps --filter "name=dev-" --format "{{.Names}}" | grep "$REPO_SLUG" | xargs -r docker rm -f`.

### Step 1: Create project
1. Generate `PROJECT_ID` with `uuidgen`.
2. POST to `/api/projects/` with `id`, `name`, and `config` including modules and task description + audit instructions (as defined in `.claude/skills/e2e-run/SKILL.md`).
3. If test includes `tg_bot` AND level is C, inject `TELEGRAM_BOT_TOKEN` from `.claude/e2e-secrets.env` (ask user to create if missing).

### Step 2: Trigger engineering
1. Generate `TASK_ID` (e.g., `eng-uuid...`).
2. POST to `/api/tasks/`.
3. Publish to `ENGINEERING_QUEUE` using `RedisStreamClient` via `docker compose exec langgraph python`.

### Step 3: Verify scaffold started
Wait 20s. Check `docker compose logs worker-manager`. If missing `scaffold` or `copier`, abort test immediately.

### Step 4: Monitor
Poll using `run_command` and review stdout using `command_status`:
- **Level A**: Poll GitHub for code push.
- **Level B**: Poll `http://localhost:8000/api/tasks/$TASK_ID` status. Review CI fixes.
- **Level C**: Poll engineering task, then deploy task.

### Step 5: Verify
- **Level A**: Inspect GitHub files and latest commit message.
- **Level B**: Print CI run status via GitHubAppClient.
- **Level C**: Check API records for deploy server. SSH to server and verify containers and logs.

### Step 6: Collect worker audit report
Fetch `AUDIT_REPORT.md` from the repo. Save raw report to `docs/e2e_results/worker_reports/<project_name>-<date>-level<X>-worker.md` (matching main report name + `-worker` suffix). These are preserved for human reference — never deleted.

### Step 7: Write E2E report
Write report to `docs/e2e_results/<project_name>-<date>.md`. Do not overwrite. Use worker audit findings to populate the `## Problems Found` section. Classify problems by type: orchestrator, template, meta, or other. Give it a backlog tag or mark as new. Commit both the main report and `docs/e2e_results/worker_reports/`.

### Step 8: Cleanup (skip if --no-cleanup)
1. Kill worker containers.
2. Delete GitHub repo via `GitHubAppClient`.
3. **Level C**: Remove app from server via SSH, delete deployment records.
4. **All levels**: Delete project from DB (`DELETE /api/projects/$PROJECT_ID`).

## Abort & Collect
If manual interrupt requested: kill worker, stop polling, collect partial results (commits/CI), write failed report, cleanup.
