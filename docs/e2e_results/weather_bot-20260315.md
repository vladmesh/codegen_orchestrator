# E2E Report: weather_bot — Deploy fixed, API endpoint missing

> **Date**: 2026-03-15
> **Project**: weather-bot (project_id: `bf2b013d-5f3a-4e20-9c89-9a5a4106811b`)
> **Story**: story-21325e9c
> **Status**: Partial Pass (deployed, but weather endpoint missing)
> **Smoke**: fail (health OK, /api/weather/{city} returns 404)
> **Worker audit**: not found

---

## Timeline

```
00:55  PO request sent (via po:input)
00:55  PO response: asked for telegram token
00:55  Replied: token will be added later
00:55  PO response: asked about access model
00:55  Replied: public bot
00:56  PO created project weather-bot + story-21325e9c
00:56  Injected TELEGRAM_BOT_TOKEN into project secrets
00:56  Scaffold complete (DRAFT → ACTIVE, workspace_ready=true)
00:56  Architect created 3 tasks (no CI check task)
00:57  task-12d247fd in_dev (Create backend weather API endpoint)
01:10  task-12d247fd done, task-ce5c2b6e done
01:13  task-891c66d6 done (Implement Telegram bot /weather command)
01:13  Deploy started — FAILED x3 (ensure_project_allocations TypeError)
01:14  Story → failed (max retries exceeded)
--- HOTFIX: deploy.py _allocate_resources —  added repo_id/service_name ---
01:16  Deploy retry — FAILED (precheck: action=feature but dir doesn't exist)
--- HOTFIX: deploy.py — added feature→create auto-fallback ---
01:17  Deploy retry — deploying...
01:19  Story → completed
01:20  Smoke test: /health OK, /api/weather/moscow 404
```

## PO Interaction

PO asked 2 clarifying questions before creating the project:
1. Telegram bot token (replied: will add later)
2. Access model (replied: public)

Created project as `weather-bot` (hyphenated) with correct modules `[backend, tg_bot]`.

## Problems Found

### Problem 1: deploy.py — _allocate_resources missing repo_id/service_name
- **Type**: orchestrator
- **Severity**: critical
- **Backlog**: `new`
- **Description**: `ensure_project_allocations()` signature was updated to require `repo_id` and `service_name`, but `_allocate_resources()` in `deploy.py` was not updated to pass them.
- **Root cause**: Incomplete refactor of `ensure_project_allocations` — caller in deploy consumer missed.
- **Suggested fix**: Fixed in this session — added `repo_id` and `service_name` lookup.

### Problem 2: deploy precheck — no feature→create fallback
- **Type**: orchestrator
- **Severity**: major
- **Backlog**: `new`
- **Description**: After allocation creates an Application record, dispatcher sets `action=feature` (because Application exists). But on first deploy, the server directory doesn't exist yet, so precheck fails with "never deployed".
- **Root cause**: `complete_stories` in task_dispatcher checks for existing Applications to decide action. But allocator creates the Application *before* deploy runs. Only create→feature fallback existed, not the reverse.
- **Suggested fix**: Fixed in this session — added `feature→create` auto-fallback in deploy precheck.

### Problem 3: Weather API endpoint not implemented
- **Type**: orchestrator
- **Severity**: major
- **Backlog**: `new`
- **Description**: `/api/weather/{city}` returns 404. The repo only has `/health` and `/users` endpoints. Developer agent created weather-related code (cache model, etc.) but didn't register the router.
- **Root cause**: Task decomposition by architect was reasonable, but developer agents may not have properly integrated their work. No CI check task was created to verify all endpoints work.
- **Suggested fix**: Architect should always append a CI/integration check task. Developer instructions should emphasize registering routes.

### Problem 4: No AUDIT_REPORT.md
- **Type**: orchestrator
- **Severity**: minor
- **Backlog**: `—`
- **Description**: None of the 3 workers created AUDIT_REPORT.md despite audit instructions in the task description.
- **Root cause**: Audit instructions are appended to the story description, but `_build_feature_task` doesn't include them in the task message sent to workers.
- **Suggested fix**: Ensure audit instructions are passed through to the worker prompt, or inject them separately.

### Problem 5: git_pull_failed — no tracking information
- **Type**: orchestrator
- **Severity**: minor
- **Backlog**: `new`
- **Description**: Worker wrapper logs `git_pull_failed` with "no tracking information for current branch" on every task start.
- **Root cause**: Scaffolder does `git init` + `git push origin main` (without `-u`), so upstream tracking is not set. Worker-manager mounts the same workspace via bind mount.
- **Suggested fix**: Fixed in this session — scaffolder now uses `git push -u origin main`, wrapper uses `git pull --rebase=false origin main`.

### Problem 6: Architect doesn't create CI check task
- **Type**: orchestrator
- **Severity**: minor
- **Backlog**: `—`
- **Description**: Architecture docs say architect "appends CI check task at the end", but no CI check task was created for this story.
- **Root cause**: Architect LLM prompt may not consistently produce a CI check task.
- **Suggested fix**: Enforce CI check task creation in architect consumer code, not just LLM instructions.

### Problem 7: Deploy retry counter not reset on story reopen
- **Type**: orchestrator
- **Severity**: minor
- **Backlog**: `new`
- **Description**: After reopening a failed story, the deploy retry counter (`deploy:{story_id}:attempts`) persists, so the next deploy attempt starts at count 4 and immediately fails.
- **Root cause**: `_handle_deploy_failure` increments counter in Redis but story reopen doesn't reset it.
- **Suggested fix**: Reset `deploy:{story_id}:attempts` key when story is reopened.
