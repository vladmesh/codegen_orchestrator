# Escort Report: random-cat-bot — Telegram bot for random cat photos

> **Date**: 2026-03-15
> **Project**: random-cat-bot (project_id: `5d70df18-54ef-47df-b4d7-e9ec06d57722`)
> **Story**: story-3e430423 — "Create random cat photo bot"
> **User**: owner_id 2
> **Modules**: backend, tg_bot
> **Mode**: escort (observed + intervened)
> **Result**: Delivered with interventions
> **Duration**: ~16 minutes (20:09 → 20:25 UTC)
> **Deployed URL**: http://80.209.235.229:8010

## Timeline

| Time (UTC) | Event |
|---|---|
| 20:09:08 | Project created by PO |
| 20:08:55 | PO → Telegram: "Создаю бота для случайных фоток котов!" (normal ack) |
| 20:09:26 | Story created, scaffold:queue + architect:queue messages published |
| 20:09:26 | PO transitions story → in_progress |
| 20:09:39 | Scaffolder: registry secrets set |
| 20:09:46 | Scaffolder: copier complete, make setup start |
| 20:09:56 | Scaffolder: scaffold complete (102 file tree), specs extracted |
| 20:09:57 | Scaffolder: branch protection set, project → active |
| ~20:10 | Scaffold push to main → CI runs → CI success webhook → deploy:queue (scaffold deploy) |
| 20:10:07 | Architect: scaffold ready (waited 40s), LLM decomposition starts |
| 20:10:27 | Architect: 1 task created (task-cc33912a) |
| 20:10:31 | **Architect CRASHED**: 422 on story/start (story already in_progress) |
| 20:10:35 | Task dispatcher picks up task → in_dev |
| 20:10:37 | Engineering worker: worker spawn requested |
| 20:11:22 | Worker container created: worker-dev-random-cat-bot-c1cf8197 |
| 20:11:23 | Claude agent starts in worker |
| 20:13:48 | **Scaffold deploy completes** (deploy-wh-28180a94, smoke: pass) |
| 20:13:53 | **FALSE NOTIFICATION #1**: PO → Telegram: "Бот готов и запущен!" (only scaffold deployed, no feature code) |
| ~20:15 | Agent commits: `6700edb feat: implement random cat photo bot` |
| 20:16:25 | Engineering worker: task done (commit 6700edb) |
| 20:16:40 | Scheduler: PR #1 created (story/story-3e430423 → main) |
| 20:16:41 | **Auto-merge FAILED**: "not allowed for this repository" |
| 20:16:41 | Story → pr_review, worker container cleaned up |
| 20:18 | CI running on PR branch |
| 20:19 | CI passed (success) |
| 20:21:40 | PO reminder fires: "check story status for random cat bot" |
| 20:21:43 | **FALSE NOTIFICATION #2**: PO → Telegram: "Уже готов и работает!" (PO checked status, saw deploy, but PR not yet merged — still scaffold code) |
| 20:20:10 | **INTERVENTION**: Enabled allow_auto_merge on repo via API |
| 20:20:14 | **INTERVENTION**: Merged PR #1 directly (auto-merge GraphQL node ID bug) |
| 20:20:14 | GitHub webhooks received (merge event) |
| 20:22:12 | CI on main triggers, webhook → deploy:queue (delayed by stale messages) |
| 20:22:27 | Deploy worker picks up deploy-wh-1b3bb89a |
| 20:22:30-40 | Deploy worker: secrets configured (9 secrets) |
| 20:22:41 | Deploy worker: deploy.yml workflow dispatched |
| 20:24:02 | Deploy workflow completed successfully |
| 20:24:02 | Smoke test: PASS (backend HTTP 200, tg_bot skipped) |
| 20:24:02 | **deployment_record_error**: 500 on service-deployments (duplicate) |
| 20:24:06 | PO → Telegram: "Обновление завершено!" (correct — real deploy with feature code) |
| 20:25:33 | **INTERVENTION**: Story manually transitioned → deploying → completed |

## Interventions

### Intervention 1: Enable auto-merge on repo
- **When**: 20:20 UTC
- **What broke**: `enable_auto_merge` GraphQL mutation failed with "Pull request Auto merge is not allowed for this repository". The scaffolder sets branch protection but doesn't enable `allow_auto_merge` in repo settings.
- **What I did**: `PATCH /repos/.../random-cat-bot` with `{"allow_auto_merge": true}` via GitHub API
- **Impact**: Unblocked auto-merge capability

### Intervention 2: Direct PR merge
- **When**: 20:20 UTC
- **What broke**: Even after enabling repo-level auto-merge, the `enable_auto_merge` GraphQL mutation failed with node ID resolution error (`Could not resolve to a node with the global id of '1'`). The function passes PR number instead of GraphQL node ID.
- **What I did**: Merged PR #1 directly via REST API (`PUT /pulls/1/merge`)
- **Impact**: Unblocked deploy pipeline

### Intervention 3: Delete stale deploy queue messages
- **When**: 20:20 UTC
- **What broke**: 4 stale deploy messages from project `033c2033...` were clogging the deploy queue
- **What I did**: Deleted 4 stale messages via debug API
- **Impact**: Ensured our deploy message would be processed promptly

### Intervention 4: Manual story completion
- **When**: 20:25 UTC
- **What broke**: Deploy worker completed successfully but didn't transition story to `completed`. The webhook deploy message had `story_id: ""` (empty string), so deploy worker couldn't link back to the story.
- **What I did**: `POST /stories/.../deploy` then `POST /stories/.../complete`
- **Impact**: Story properly marked as completed

## Worker Reports

### Task 1: Implement random cat photo bot with admin access control
- **Task ID**: task-cc33912a
- **Result**: completed
- **Commit**: 6700edb
- **Duration**: 6.9 minutes

**What Worked:**
- python-telegram-bot v21.4 provides clean APIs for inline keyboards and callback queries
- TheCatAPI (api.thecatapi.com) works well as the cat photo source — returns JSON with image URLs, no API key required
- Existing project structure and conventions were clear and easy to follow
- ruff linting and formatting worked smoothly

**Issues Encountered:**
1. Pre-push hook cannot find ruff (minor, workaround: add .venv/bin to PATH)
2. pytest not directly executable in tg_bot venv (minor, workaround: python -m pytest)

## Problems Found

### Problem 1: Architect crashes on story already in_progress
- **Type**: orchestrator
- **Severity**: major
- **Status**: needs-fix
- **Backlog**: new
- **Description**: Architect tries to `POST /stories/{id}/start` after creating tasks, but PO already transitions story to `in_progress` before architect runs. This causes 422 and crashes the architect.
- **Root cause**: Race condition — PO and architect both try to start the story. Architect's `transition_story(story_id, "start")` call happens after PO already did it.
- **Evidence**: `httpx.HTTPStatusError: Client error '422 Unprocessable Entity' for url 'http://api:8000/api/stories/story-3e430423/start'`
- **Suggested fix**: Architect should catch 422 on story/start and treat it as a no-op (story already started by PO). Or remove the transition from architect since PO handles it.

### Problem 2: Scaffolder doesn't enable allow_auto_merge on repo
- **Type**: orchestrator
- **Severity**: major
- **Status**: fixed-during-escort
- **Backlog**: new
- **Description**: Scaffolder sets branch protection (require CI + PRs) but doesn't enable `allow_auto_merge` in repo settings. This means `enable_auto_merge` GraphQL mutation fails for every new project.
- **Root cause**: Missing `allow_auto_merge: true` in scaffolder's repo configuration step
- **Evidence**: `"Pull request Auto merge is not allowed for this repository"`
- **Suggested fix**: Add `PATCH /repos/{owner}/{repo}` with `{"allow_auto_merge": true}` in scaffolder after setting branch protection

### Problem 3: enable_auto_merge passes PR number instead of GraphQL node ID
- **Type**: orchestrator
- **Severity**: major
- **Status**: needs-fix
- **Backlog**: new
- **Description**: `GitHubAppClient.enable_auto_merge()` passes PR number (e.g., `1`) to GraphQL `enablePullRequestAutoMerge` mutation, but GraphQL requires the node ID (e.g., `PR_kwDON...`)
- **Root cause**: Function doesn't fetch PR node_id before calling GraphQL
- **Evidence**: `Could not resolve to a node with the global id of '1'`
- **Suggested fix**: Fetch PR details via REST first to get `node_id`, then pass to GraphQL mutation

### Problem 4: Webhook deploy message has empty story_id
- **Type**: orchestrator
- **Severity**: major
- **Status**: needs-fix
- **Backlog**: new
- **Description**: When webhook triggers deploy after PR merge, the deploy queue message has `story_id: ""`. Deploy worker can't transition story to completed.
- **Root cause**: Webhook handler doesn't resolve story_id from the PR branch name (`story/story-3e430423`) when publishing to deploy:queue
- **Evidence**: Deploy message `story_id: ""` in queue; story stuck in `pr_review` after successful deploy
- **Suggested fix**: Webhook handler should extract story_id from PR head branch (parse `story/{story_id}` pattern) and include it in the deploy message

### Problem 5: Deployment record 500 error (duplicate)
- **Type**: orchestrator
- **Severity**: minor
- **Status**: needs-fix
- **Backlog**: new
- **Description**: Deploy worker got 500 when creating service-deployment record, likely because the previous deploy (scaffold-triggered) already created one for this project/server/port combo.
- **Root cause**: No upsert logic in service-deployments — second deploy for same project conflicts
- **Evidence**: `deployment_record_error: Server error '500 Internal Server Error' for url 'http://api:8000/api/service-deployments/'`
- **Suggested fix**: Use upsert (create-or-update) for service-deployment records

### Problem 6: Stale deploy queue messages from old projects
- **Type**: orchestrator
- **Severity**: minor
- **Status**: fixed-during-escort
- **Backlog**: —
- **Description**: 4 stale messages in deploy:queue from project `033c2033...` that were never consumed
- **Root cause**: Previous deploy failures or test runs didn't clean up their messages
- **Suggested fix**: Add TTL-based cleanup for old queue messages, or dead-letter queue

### Problem 7: OpenTelemetry context detach warning
- **Type**: orchestrator
- **Severity**: warning
- **Status**: known-issue
- **Backlog**: —
- **Description**: `Failed to detach context: ValueError: Token was created in a different Context` appears in both engineering-worker and deploy-worker after every job
- **Root cause**: OpenTelemetry context propagation issue with async LangGraph execution
- **Suggested fix**: Investigate OTel context propagation in LangGraph async nodes; likely harmless but noisy

### Problem 8: Scaffold push to main triggers premature deploy + false "ready" notification
- **Type**: orchestrator
- **Severity**: major
- **Status**: needs-fix
- **Backlog**: new
- **Description**: Scaffolder pushes to main → CI passes → webhook triggers deploy of bare scaffold code (no feature). Deploy-worker deploys successfully (smoke passes because scaffold has valid health endpoint), then sends `po:proactive` message. User receives "Бот готов и запущен!" at 20:13:53 when the feature code hasn't even been written yet (engineering task started at 20:11, finished at 20:16).
- **Root cause**: CI success webhook doesn't distinguish scaffold commits from feature commits. Any green CI on main triggers deploy:queue.
- **Evidence**: `deploy-wh-28180a94` completed at 20:13:48 with `deployed_url: http://80.209.235.229:8010`. Proactive message at 20:13:53: "Ваш бот ready-cat-bot готов и запущен!"
- **Suggested fix**: Either (a) skip deploy webhook for scaffold commits (e.g. check if commit message matches scaffold pattern), or (b) don't trigger deploy webhook until story has at least one completed task, or (c) scaffolder should not push directly to main — push to a setup branch that gets merged later.

### Problem 9: PO reminder triggers second false "ready" notification
- **Type**: orchestrator
- **Severity**: major
- **Status**: needs-fix
- **Backlog**: new
- **Description**: PO agent has a reminder that fires to check story status. At 20:21:40 the reminder fired, PO checked the story, saw a previous successful deploy existed, and sent "Уже готов и работает!" to user. But at this point the PR wasn't merged yet — the deployed code was still just the scaffold.
- **Root cause**: PO reminder logic doesn't verify whether the *latest* code (post-PR-merge) is deployed. It sees any past deploy success and reports it.
- **Evidence**: `reminder_fired` at 20:21:40, `proactive_message_sent` at 20:21:43 (len=108): "Бот random-cat-bot уже готов и работает!"
- **Suggested fix**: PO should check that story status is `completed` (not just that a deploy happened) before reporting success. Or: don't send proactive "ready" messages from reminders — only from deploy completion events.

### Problem 10: Smoke test skips tg_bot verification
- **Type**: orchestrator
- **Severity**: recommendation
- **Status**: known-issue
- **Backlog**: —
- **Description**: Smoke test skips tg_bot module check because "Telethon env vars not configured"
- **Root cause**: Smoke test uses Telethon for tg_bot verification, but Telethon API credentials aren't set up
- **Suggested fix**: Consider alternative tg_bot smoke check (e.g., Telegram getMe API call with the bot token) that doesn't require Telethon

## Interference Analysis

No other stories were actively processing during this escort. 17 stories are in `in_progress` status (stale from previous runs) but none had active workers or queue messages.

## Metrics

- **Tasks**: 1 created, 1 completed, 0 failed
- **Engineering time**: 6.9m (single task)
- **CI cycles**: 1 (passed on first try)
- **Deploy attempts**: 2 (1 scaffold deploy + 1 PR merge deploy, both successful)
- **Manual interventions**: 4
- **Worker reports collected**: 1/1
- **False user notifications**: 2 out of 3 "ready" messages were premature

## Recommendations

1. **[MAJOR]** Prevent scaffold commit from triggering deploy — user gets false "ready" notification before any feature code exists
2. **[MAJOR]** Fix PO reminder logic — should not report "ready" unless story is `completed`
3. **[MAJOR]** Fix scaffolder to enable `allow_auto_merge` on new repos — blocks every new project's auto-merge flow
4. **[MAJOR]** Fix `enable_auto_merge` to use GraphQL node_id — even with repo setting fixed, the function is broken
5. **[MAJOR]** Webhook deploy handler should extract and include story_id from PR branch — without this, story never transitions to completed
6. **[MAJOR]** Architect should gracefully handle 422 on story/start — prevents crash when PO already started the story
7. **[MINOR]** Use upsert for service-deployment records — prevents 500 on re-deploy
8. **[RECOMMENDATION]** Add tg_bot smoke check using getMe API instead of Telethon
