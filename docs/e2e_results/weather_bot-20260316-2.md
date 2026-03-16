# E2E Report: weather_bot — Failed (weather endpoint not working)

> **Date**: 2026-03-16
> **Project**: weather-bot (project_id: `1bf09e00-3c06-4b3b-8917-17f4772840d0`)
> **Story**: story-bd6c1cde
> **Status**: Failed
> **Feature phase**: skipped
> **Smoke**: partial (`/health` OK, `/weather/{city}` 404)
> **Worker reports**: collected (2)

---

## Timeline

```
08:50  PO: project creation message sent
08:51  PO: asked for Telegram bot token → sent token
08:51  PO: asked about access control → replied "public"
08:51  PO: project created (weather-bot), pipeline started
08:51  Telegram bot token injected into project secrets
08:52  Scaffold: complete (project=active, workspace_ready=true)
08:53  Architect: 2 tasks created (backend API + telegram bot)
10:54  task-f75f7ccd: todo → in_dev (backend weather API, worker started)
11:01  task-f75f7ccd: in_dev → done (~7 min)
11:01  task-37fc6976: todo → in_dev (telegram bot /weather command)
11:04  task-37fc6976: in_dev → done (~3 min)
11:04  Story → pr_review (PR #1 created, auto-merge enabled)
11:05  CI passed (lint-and-test=success, build-and-push=skipped)
11:05  Auto-merge blocked: branch protection requires check "ci", actual check is "lint-and-test"
11:07  INTERVENTION: updated branch protection to require "lint-and-test" instead of "ci"
11:08  Story → deploying (PR auto-merged, webhook received)
11:10  Story → completed (deploy workflow succeeded)
11:11  Verification: containers not running on server (port conflict with old containers)
11:13  INTERVENTION: removed old weather-bot containers, started via compose
11:14  Backend crash-looping: can't resolve host 'db' (container not on network)
11:17  INTERVENTION: full compose down + up — all containers healthy
11:17  Smoke test: /health OK, /docs OK
11:17  Weather API: /weather/moscow → 404, /api/weather/moscow → 404
11:18  Investigation: weather router exists in code but not loaded by FastAPI (import error)
11:18  OpenAPI shows only: /health, /users CRUD — no weather endpoints
```

Total duration: ~27 minutes (engineering: 10 min, deploy+verification: ~10 min)

## PO Interaction

- PO asked for Telegram bot token (expected for tg_bot module)
- PO asked about access control (public/private) — answered "public"
- 3 message exchanges before pipeline started — acceptable for tg_bot project
- PO created project name as `weather-bot` (hyphenated)

## Problems Found

### Problem 1: PR auto-merge blocked by check name mismatch (recurring)
- **Type**: template
- **Severity**: critical
- **Backlog**: was marked done in previous report but recurring
- **Description**: Branch protection requires check named `ci`, but ci.yml job is named `lint-and-test`. Auto-merge never triggers.
- **Root cause**: Branch protection setup script uses `ci` as context name, but ci.yml workflow job is named `lint-and-test`
- **Suggested fix**: The previous fix (commit 6d9eac8) didn't persist — new repos still get the old protection. Fix must be in the scaffolder or deploy-worker branch protection setup code.

### Problem 2: Deploy left orphan containers with different compose project name
- **Type**: orchestrator
- **Severity**: major
- **Backlog**: `new`
- **Description**: Deploy workflow started containers with project name `weather-bot` (e.g. `weather-bot-backend-1`). But when running `docker compose` from `/opt/services/weather-bot/infra/`, the default project name is `infra`, creating `infra-backend-1`. This caused port 8012 conflict and networking issues.
- **Root cause**: deploy.yml runs `docker compose` without explicit `-p` project name, so it picks up directory name. The deploy script likely runs from a different working directory than expected.
- **Suggested fix**: deploy.yml should always set `-p $PROJECT_NAME` or the compose file should have a `name:` field.

### Problem 3: Weather endpoint not loaded — router import fails silently
- **Type**: template
- **Severity**: critical
- **Backlog**: `new`
- **Description**: Weather router code exists in the repo at `services/backend/src/app/api/routers/weather.py` but it's not loaded by FastAPI. OpenAPI spec only shows `/health` and `/users` endpoints. The weather router imports `from services.backend.src.controllers.weather import WeatherController` using absolute paths that work in dev but fail in the container where PYTHONPATH is `/app`.
- **Root cause**: The generated router uses absolute import paths (`services.backend.src.controllers.weather`) but the Docker container's PYTHONPATH expects relative imports (`src.controllers.weather`). FastAPI silently skips routers that fail to import.
- **Root cause (deeper)**: The framework generates routers with absolute paths based on the project structure, but the Dockerfile sets WORKDIR to `/app/services/backend` where the import should be relative.
- **Suggested fix**: Fix import path generation in the framework to use the correct relative imports for the container context. Or ensure PYTHONPATH in the Dockerfile matches what the framework generates.

### Problem 4: Long gap between scaffold completion and task dispatch (~2 hours)
- **Type**: orchestrator
- **Severity**: major
- **Backlog**: `new`
- **Description**: Scaffold completed at 08:52 but worker didn't start until 10:54 — a ~2 hour gap. Architect should have created tasks within minutes.
- **Root cause**: Unknown. Possibly architect queue was slow to consume, or there was a processing delay. The architect container logs were not captured during this gap.
- **Suggested fix**: Investigate architect/scheduler latency. Add monitoring alerts for tasks not appearing within 5 min of scaffold completion.

### Problem 5: TG bot conflict error on deploy
- **Type**: other
- **Severity**: minor
- **Backlog**: `—`
- **Description**: TG bot shows "Conflict: terminated by other getUpdates request" — another instance is polling with the same token.
- **Root cause**: The same bot token was used in a previous E2E test deployment that wasn't fully cleaned up, or the old `weather-bot-tg_bot-1` container was still running when `infra-tg_bot-1` started.
- **Suggested fix**: Pre-flight cleanup should stop ALL containers matching the project name on all servers, not just those in `/opt/services/`.

---

## Worker Reports Summary

Both workers completed successfully. Common issue: pre-push hook can't find `ruff` (need to add `.venv/bin` to PATH). Workers used the spec-first workflow correctly (task 1 created weather spec + ran `make generate-from-spec`).

The code itself was generated correctly — the issue is in how imports are resolved at runtime in the container, not in the worker's implementation.
