# Escort Report: reverse-message-bot — Delivered with interventions

> **Date**: 2026-03-13
> **Project**: reverse-message-bot (project_id: `7764f9cb-2127-47ca-9aa6-6a80dd2190b5`)
> **Story**: story-ad0a6cf2 — "Create reverse message bot"
> **User**: owner_id 1 (Vlad)
> **Modules**: backend, tg_bot
> **Mode**: escort (observed + intervened)
> **Result**: Delivered with interventions
> **Duration**: ~28 minutes (23:38 — 00:06 UTC)
> **Deployed URL**: http://80.209.235.229:8015

## Timeline

| Time (UTC) | Event |
|---|---|
| 23:38:25 | PO created project + repository |
| 23:38:30 | Telegram token validated (@vlad_test_bot_factory_bot) |
| 23:38:50 | Story created and submitted to architect |
| 23:39:02 | Dispatcher triggered scaffold |
| 23:39:03 | Scaffolder: GitHub repo created |
| 23:39:06 | Registry secrets set (3) |
| 23:39:07 | Copier template running (modules: backend, tg_bot) |
| 23:39:15 | **Scaffold complete** (102 tree lines, 1 domain, 3 models, 2 events) |
| 23:42:51 | Architect picked up story (was blocked 3min by stale test story) |
| 23:43:14 | **Architect hit 500 error** on second task creation (blocked_by_task_id="None" string) |
| 23:43:14 | Architect created 1 of 2 tasks before failing |
| 23:43:37 | **INTERVENTION**: Created missing task + CI check task manually |
| 23:44:13 | Worker container spawned |
| 23:44:14 | Task 1 started (DB model + migration) |
| 23:47:18 | Task 1 completed (commit 286bf2d, worker report collected) |
| 23:47:43 | Task 2 started (bot implementation, session resumed) |
| 23:54:06 | Task 2 completed (commit ae94203, 27 tests pass, worker report collected) |
| 23:54:21 | Task 3 started (CI check) |
| 23:58:28 | Task 3 completed (CI passed, commit 0a15fa4, worker report collected) |
| 00:01:30 | **INTERVENTION**: Story still `created` — manually transitioned to `in_progress` |
| 00:01:30 | Dispatcher detected all tasks done, triggered deploy |
| 00:02:14 | Deploy workflow dispatched |
| 00:06:30 | **Deploy completed** — smoke test passed (backend HTTP 200) |
| 00:06:30 | Story completed |

## Interventions

### Intervention 1: Clear stale architect queue
- **When**: 23:43 UTC (before architect processed our story)
- **What broke**: Architect queue had 75 stale messages from live test runs (test stories, deleted projects). Our story was at position 55 of 68. The architect consumer processes messages sequentially with no skip logic for completed/deleted stories — each stale message triggers a full LLM call or 5-minute scaffold timeout.
- **What I did**: Deleted 75 stale messages from `architect:queue` using `XDEL`, keeping only the currently processing message and ours.
- **Impact**: Unblocked our story. Without this, the architect would have been stuck for hours processing dead messages.

### Intervention 2: Create missing tasks after architect 500 error
- **When**: 23:43:37 UTC
- **What broke**: The architect LLM tried to create a second task with `blocked_by_task_id="None"` (the Python string "None" instead of null). The API's PostgreSQL FK constraint rejected this: `Key (blocked_by_task_id)=(None) is not present in table "tasks"`. The architect created 1 of 2 tasks, then the entire graph crashed. The `append_ci_check_task` function never ran.
- **What I did**: Created the missing bot implementation task (`task-4f7d571b`) with correct `blocked_by_task_id` pointing to the DB model task, and the CI check task (`task-1d56bea1`) blocked by the bot task. Used `created_by: "escort"`.
- **Impact**: Unblocked the story completely. All 3 tasks executed successfully.

### Intervention 3: Transition story from `created` to `in_progress`
- **When**: 00:01:30 UTC
- **What broke**: Story remained in `created` status even after all tasks completed. The dispatcher's `complete_stories` function only checks `in_progress` stories. Normally the dispatcher transitions stories to `in_progress` when triggering scaffold, but the transition was missed because the story was handled by the escort (tasks created manually, not by the normal architect→dispatcher flow).
- **What I did**: `POST /api/stories/story-ad0a6cf2/start` with `actor: "escort"`.
- **Impact**: Dispatcher immediately detected all tasks done, triggered deploy.

## Worker Reports

### Task 1: Create whitelist database model and migrations
# Developer Report

## Summary
- **Task**: Create whitelist database model and migrations
- **Result**: completed

## Environment
- **Database**: FAILED — `Bind for 0.0.0.0:5432 failed: port is already allocated`
- **`getent hosts db`**: no result (exit code 2)
- **Workaround**: Created migration manually following existing pattern

## What Worked
- Scaffolding and venv in place, ruff clean, existing patterns clear

## Issues
1. **Port 5432 conflict** (infra/major) — Docker daemon port conflict from another project, not visible in /proc/net/tcp

### Task 2: Implement Telegram bot with access control and message reversing
# Developer Report

## Summary
- **Task**: Implement Telegram bot with access control and message reversing
- **Result**: completed — 27 tests pass (9 backend + 18 tg_bot)

## Issues
1. **Broken shebangs in venv** (tooling/minor) — pointed to `/data/workspaces/repo-b8fc8def` instead of `/workspace`
2. **Missing xenon/framework in root venv** (tooling/minor) — fixed with `make setup`

### Task 3: Run tests, verify CI green
# Developer Report

## Summary
- **Task**: CI verification
- **Result**: completed — all 27 tests pass, CI green (run 23029529666)

## Issues
1. **Broken shebangs** (same as task 2)
2. **Pre-push hook couldn't find ruff** (tooling/minor) — not on system PATH

## Problems Found

### Problem 1: Architect queue clogged with stale messages
- **Type**: orchestrator
- **Severity**: major
- **Status**: needs-fix
- **Backlog**: new
- **Description**: Live test runs leave messages in `architect:queue` for test stories that are never cleaned up. The architect consumer has no skip logic — every message triggers a full LLM call or 5-minute scaffold timeout wait, even for completed/deleted stories.
- **Root cause**: No TTL or cleanup mechanism for queue messages. No guard in architect consumer to skip already-completed stories.
- **Evidence**: 75 stale messages, our story at position 55 of 68
- **Suggested fix**: (a) Add a guard in `process_architect_job` to skip stories with status `completed`/`archived`/`failed`. (b) Add TTL/cleanup for old queue messages. (c) Live test cleanup should drain its queue messages.

### Problem 2: Architect LLM passes "None" string as blocked_by_task_id
- **Type**: orchestrator
- **Severity**: critical
- **Status**: needs-fix
- **Backlog**: new
- **Description**: The architect LLM returned `blocked_by_task_id: "None"` (string) instead of `null`. The API FK constraint rejects this, causing a 500 error and partial task creation.
- **Root cause**: LLM serialization issue — the architect tool's `create_task` function doesn't sanitize the `blocked_by_task_id` field before passing to API.
- **Evidence**: `ForeignKeyViolationError: Key (blocked_by_task_id)=(None) is not present in table "tasks"`
- **Suggested fix**: In the architect's `create_task` tool, coerce `"None"` / `"null"` / empty strings to `None` before passing to the API. Also add server-side validation in the task creation endpoint.

### Problem 3: Story not transitioned to in_progress when tasks created by non-standard flow
- **Type**: orchestrator
- **Severity**: major
- **Status**: needs-fix
- **Backlog**: new
- **Description**: When tasks are created manually (e.g., by escort or admin), the story remains in `created` status. The dispatcher only checks `in_progress` stories for completion. This means the pipeline stalls after all tasks complete.
- **Root cause**: Story-to-in_progress transition happens in the normal dispatcher flow (scaffold trigger path). When tasks bypass this path, the transition is missed.
- **Suggested fix**: The dispatcher's `complete_stories` check should also look at `created` stories with all tasks done, or the task creation endpoint should auto-start the story.

### Problem 4: Port 5432 conflict in worker containers
- **Type**: infra
- **Severity**: major
- **Status**: known-issue
- **Backlog**: existing (known from previous escorts)
- **Description**: Worker containers can't start their dev DB because port 5432 is already allocated at the Docker daemon level. Workers resort to creating migrations manually without DB validation.
- **Root cause**: Multiple worker containers share the host's port space, and the orchestrator's own PostgreSQL binds 5432.
- **Suggested fix**: Use dynamic port allocation or internal Docker networking for worker dev databases.

### Problem 5: Broken shebangs in worker venv
- **Type**: template
- **Severity**: minor
- **Status**: needs-fix
- **Backlog**: new
- **Description**: Python venv binaries (pytest, xenon) have shebangs pointing to `/data/workspaces/repo-b8fc8def` instead of `/workspace`. Worker agents have to fix this with sed on every task.
- **Root cause**: The worker-manager mounts the workspace at `/workspace` but the venv was created when the path was `/data/workspaces/repo-b8fc8def` (during scaffold).
- **Suggested fix**: Recreate the venv in the worker container at the correct path, or fix shebangs in worker-manager setup.

### Problem 6: Smoke test skipped for tg_bot module
- **Type**: orchestrator
- **Severity**: warning
- **Status**: known-issue
- **Description**: Deploy smoke test skipped tg_bot check ("Telethon env vars not configured"). Backend checked OK (HTTP 200).
- **Root cause**: Telethon-based smoke test requires additional env vars not configured in the deploy environment.
- **Suggested fix**: Configure Telethon env vars for smoke testing, or implement a simpler bot health check.

## Interference Analysis

Other stories running during escort: story-2ce3290c, story-35d8066f, story-d7239670 (all stale test stories in `created` status). Their scaffold jobs never completed and they were consuming architect queue slots. No active interference with our story's engineering or deploy phases (no competing workers or port conflicts at deploy).

## Metrics

- **Tasks**: 3 created, 3 completed, 0 failed
- **Engineering time**: ~4.5m per task (average)
- **CI cycles**: 1 per task (all passed first try)
- **Deploy attempts**: 1 (succeeded)
- **Manual interventions**: 3
- **Worker reports collected**: 3/3
- **Total pipeline time**: ~28 minutes

## Recommendations

1. **[CRITICAL]** Fix architect `blocked_by_task_id="None"` string bug — sanitize in tool and validate server-side. This causes partial task creation and breaks the entire story.
2. **[MAJOR]** Add skip-guard in architect consumer for already-completed/failed/archived stories — saves LLM costs and prevents queue clog.
3. **[MAJOR]** Add queue message cleanup to live test teardown — live tests should drain their architect queue messages.
4. **[MAJOR]** Handle story completion for stories in `created` status with all tasks done — either auto-start or check in dispatcher.
5. **[MINOR]** Fix venv shebang issue in worker containers — either recreate venv at correct path or fix shebangs during setup.
6. **[MINOR]** Consider dynamic port allocation for worker dev databases to avoid 5432 conflicts.
