# E2E Report: weather_bot — Deploy OK, QA bug

> **Date**: 2026-03-19
> **Project**: weather-bot (project_id: `f5ffe4a7-90da-4f73-86ca-09f4b529c288`)
> **Story**: story-eedb8dc7
> **Status**: Passed (with manual intervention)
> **Feature phase**: skipped
> **Smoke**: pass (health + /api/weather/moscow + cache verified)
> **Worker reports**: collected (2)

---

## Timeline

```
00:58  PO message sent (create weather_bot with backend + tg_bot)
00:58  PO responded — asked for bot token
00:58  Bot token sent, PO confirmed (@factory_e2e_test_bot)
00:59  Project created (weather-bot, status=draft)
00:59  Scaffold complete (draft → active, workspace_ready=true)
01:02  Architect created 2 tasks:
       - task-9db9ce1e: Implement backend weather API with PostgreSQL caching
       - task-6efad996: Implement Telegram bot /weather command
01:01  Task 1 dispatched to worker (in_dev)
03:06  Task 1 done
03:07  Task 2 dispatched (in_dev)
03:10  Task 2 done
~01:11 PR #1 merged (story/story-eedb8dc7 → main)
~01:14 Deploy succeeded (port 8012 on 80.209.235.229)
01:14  QA started — FAILED (wrong project_name: "codegen_orchestrator")
01:14  QA created fix task (task-e3ac149e), story → in_progress
01:15  [intervention] Cancelled stale QA fix task
01:18  [intervention] Verified deploy manually — all healthy
01:18  [intervention] Story → completed
```

## PO Interaction

- PO created project as `weather-bot` (hyphenated, as expected)
- PO asked for bot token, validated it, saved secrets
- Smooth 2-message interaction, no issues

## Verification Results

- **Health**: `GET /health` → `{"status": "ok"}` ✅
- **Weather API**: `GET /api/weather/moscow` → `{"city": "moscow", "temperature": 32.9, "humidity": 81, "description": "Foggy", "cached_at": "..."}` ✅
- **Caching**: Second request returned same `cached_at` timestamp ✅
- **CI**: green on main ✅
- **Containers**: 4/4 up (backend, db, redis, tg_bot), 0 restarts ✅

## Problems Found

### Problem 1: QA runner uses wrong project_name — resolves to unrelated application

- **Type**: orchestrator
- **Severity**: critical
- **Backlog**: new
- **Description**: QA runner (`services/langgraph/src/consumers/qa.py:37-44`) calls `api_client.list_applications({"project_id": project_id})`, but the API returns ALL applications, not filtered by project_id. `apps[0]` picks up `codegen_orchestrator` (id=5) instead of `weather-bot` (id=17). QA then SSHes to server and runs `cd /opt/services/codegen_orchestrator` which doesn't exist, causing QA to fail and create a bogus fix task.
- **Root cause**: Applications API filter by `project_id` doesn't work — returns all applications. The application records are not properly associated with projects (they're linked through repos, but the filter chain is broken).
- **Suggested fix**: Either fix the `project_id` filter in the applications API, or change QA runner to use `service-deployments` API instead (`/api/service-deployments/?project_id=X`) which correctly filters and returns `service_name=weather-bot`.

### Problem 2: git_pull_failed warning on first task

- **Type**: orchestrator
- **Severity**: minor
- **Backlog**: —
- **Description**: Worker wrapper logs `git_pull_failed` with `fatal: couldn't find remote ref story/story-eedb8dc7` when the first task starts. Branch doesn't exist on remote yet.
- **Root cause**: `_git_pull()` in `wrapper.py:344-358` runs unconditionally before every agent run. For the first task, the story branch hasn't been pushed yet.
- **Suggested fix**: Suppress warning when the branch doesn't exist on remote (expected for first task). Or skip pull if `git ls-remote` shows no such ref.

### Problem 3: 18 stale in_progress stories in database

- **Type**: orchestrator
- **Severity**: minor
- **Backlog**: —
- **Description**: `GET /api/stories/?status=in_progress` returns 18 stories from previous runs. Dispatcher iterates all of them every 30s cycle, adding latency.
- **Root cause**: Pre-flight cleanup doesn't clean stories from other projects. `make nuke` was skipped (--no-nuke).
- **Suggested fix**: Not a bug per se — `make nuke` would fix it. But dispatcher could benefit from only checking stories updated in the last 24h.
