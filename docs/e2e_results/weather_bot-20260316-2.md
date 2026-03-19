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
08:53  Architect: 2 tasks created (backend API + telegram bot, first already in_dev)
       (monitoring gap — polling started at 10:54, tasks were already in progress)
10:54  task-f75f7ccd: in_dev (backend weather API, worker running)
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
- **Backlog**: **fixed** (283ae01, 7454ab1)
- **Description**: Branch protection requires check named `ci`, but ci.yml job is named `lint-and-test`. Auto-merge never triggers.
- **Root cause**: Hotfix 6d9eac8 changed the default in `shared/clients/github.py`, but scaffolder passed `required_checks=["ci"]` explicitly — overriding the default. Also removed default entirely (fail-fast: `required_checks` is now a required argument).
- **Fix**: scaffolder `"ci"` → `"lint-and-test"` + removed default from `update_branch_protection` signature.

### Problem 2: Deploy left orphan containers with different compose project name
- **Type**: orchestrator
- **Severity**: major
- **Backlog**: **fixed** (service-template 5236128)
- **Description**: Deploy workflow started containers with project name `weather-bot` (e.g. `weather-bot-backend-1`). But when running `docker compose` from `/opt/services/weather-bot/infra/`, the default project name is `infra`, creating `infra-backend-1`. This caused port 8012 conflict and networking issues.
- **Root cause**: No `name:` field in compose file — project name depended on directory or `-p` flag.
- **Fix**: Added `name: {{ project_slug }}` to `compose.base.yml.jinja` (single source of truth), removed redundant `-p` from `deploy.yml.jinja`.

### Problem 3: Weather endpoint not loaded — router import fails silently
- **Type**: template
- **Severity**: critical
- **Backlog**: `new`
- **Description**: Weather router code exists in the repo at `services/backend/src/app/api/routers/weather.py` but it's not loaded by FastAPI. OpenAPI spec only shows `/health` and `/users` endpoints. The weather router imports `from services.backend.src.controllers.weather import WeatherController` using absolute paths that work in dev but fail in the container where PYTHONPATH is `/app`.
- **Root cause**: The generated router uses absolute import paths (`services.backend.src.controllers.weather`) but the Docker container's PYTHONPATH expects relative imports (`src.controllers.weather`). FastAPI silently skips routers that fail to import.
- **Root cause (deeper)**: The framework generates routers with absolute paths based on the project structure, but the Dockerfile sets WORKDIR to `/app/services/backend` where the import should be relative.
- **Suggested fix**: Fix import path generation in the framework to use the correct relative imports for the container context. Or ensure PYTHONPATH in the Dockerfile matches what the framework generates.

### Problem 4: TG bot conflict error on deploy
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
