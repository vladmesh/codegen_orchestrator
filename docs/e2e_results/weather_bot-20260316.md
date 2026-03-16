# E2E Report: weather_bot — Passed (with deploy workarounds)

> **Date**: 2026-03-16
> **Project**: weather-bot (project_id: `240d7206-0b58-4f5c-993c-e6891a4de32c`)
> **Story**: story-800beb32
> **Status**: Passed (with interventions)
> **Feature phase**: skipped
> **Smoke**: pass (`/health` OK, `/api/weather/moscow` returns data)
> **Worker reports**: collected (3)

---

## Timeline

```
00:58  PO: project created (weather-bot), asked follow-up questions
00:59  PO: follow-up → empty response (po_empty_response_fallback)
00:59  PO: 2nd follow-up → story-800beb32 created, status=in_progress
00:59  Telegram bot token injected into project secrets
00:00  Scaffold: already complete (project=active, workspace_ready=true)
00:01  Architect: 2 tasks created (backend API + telegram bot)
00:01  task-620c349f: todo → in_dev (backend weather API)
00:11  task-620c349f: in_dev → done (~10 min)
00:11  task-d2289f72: todo → in_dev (telegram bot commands)
00:16  task-d2289f72: in_dev → done (~5 min)
00:16  Dispatcher: PR #1 created (story/story-800beb32 → main), story → pr_review
00:17  CI on PR: FAILED (lint-and-test passed, but required check name mismatch)
00:17  Fix task task-e316152b auto-created (CI failure), story → in_progress
00:19  task-e316152b: backlog → todo (manual nudge) → in_dev → done (~4 min)
00:22  CI rerun: PASSED. PR auto-merge stuck (check name mismatch)
00:28  INTERVENTION: manually merged PR via API
00:28  Webhook not received (new repo, no webhook configured)
00:29  INTERVENTION: manually transitioned story to deploying + published deploy message
00:31  Deploy workflow triggered, secrets configured (9 secrets)
00:31  deploy.yml FAILED: backend container healthcheck timeout
00:32  Deploy-worker auto-retried (rerun failed jobs) → failed again
00:33  Deploy-worker created deploy-fix task → engineering queue
00:34  Fix worker started (useless — SSH/infra issue, not code)
00:41  INTERVENTION: killed fix worker (7 min, no commits, no progress)
00:50  INTERVENTION: manually started containers on server via SSH — all OK
00:50  Smoke test: /health OK, /api/weather/moscow returns cached weather data
00:50  Story → completed (manual)
```

Total duration: ~52 minutes (engineering: 15 min, rest: interventions + deploy issues)

## PO Interaction

- First message created project + repo but PO asked follow-up questions about bot token and access
- Second message got `po_empty_response_fallback` warning — PO notified user but returned empty response
- Third message successfully created story and started pipeline
- PO created project name as `weather-bot` (hyphenated), not `weather_bot` (underscored) as requested

## Problems Found

### Problem 1: PR auto-merge blocked by check name mismatch
- **Type**: template
- **Severity**: critical
- **Backlog**: `done` — branch protection now requires `lint-and-test` (6d9eac8)
- **Description**: Branch protection requires check named `ci`, but workflow jobs are named `lint-and-test` and `build-and-push`. Auto-merge never triggers.
- **Root cause**: ci.yml workflow file defines jobs with different names than what branch protection expects
- **Suggested fix**: Either rename jobs in ci.yml to match (`ci`), or update branch protection setup to use actual job names (`lint-and-test`)

### Problem 2: Webhook not received for new repos
- **Type**: orchestrator
- **Severity**: critical
- **Backlog**: `done` — dispatcher now polls GitHub for merged PRs every 30s, no webhook needed (26685e6)
- **Description**: After PR merge, no webhook was received by the API. Story stayed in `pr_review` indefinitely.
- **Root cause**: New repos created by scaffolder likely don't have the organization-level webhook configured, or the webhook URL isn't set up for this repo.
- **Suggested fix**: ~~Ensure webhook is configured at org level or scaffolder adds repo webhook during creation.~~ Replaced with polling: dispatcher checks stories in `pr_review`, queries GitHub for merged PRs, triggers deploy if found.

### Problem 3: Deploy fix task sent to engineering for infra issue
- **Type**: orchestrator
- **Severity**: major
- **Backlog**: `done` — LLM classifier (haiku) triages CODE vs INFRA before dispatching
- **Description**: Deploy failed because backend container didn't pass healthcheck in time. Deploy-worker created a fix task for engineering, but this is an infra/timing issue, not a code issue. Worker ran 7+ minutes without producing anything useful.
- **Root cause**: Deploy-worker doesn't distinguish between code failures (import errors, crashes) and infra failures (healthcheck timeout, SSH issues). All failures get dispatched to engineering.
- **Suggested fix**: Classify deploy failure type: if step is "Deploy via SSH" and containers started but healthcheck failed, retry deploy (not engineering). Only create engineering fix task for build failures or application crashes with clear error logs.

### Problem 4: Fix task created in `backlog` status, not picked up by dispatcher
- **Type**: orchestrator
- **Severity**: minor
- **Backlog**: `done` — fix tasks now created with status=todo (6d9eac8)
- **Description**: CI fix task `task-e316152b` was created with status `backlog`. Dispatcher only picks up `todo` tasks. Required manual transition.
- **Root cause**: Webhook handler creates fix tasks in `backlog` status, expecting manual triage.
- **Suggested fix**: Auto-transition CI fix tasks to `todo` since they're auto-generated and should be processed immediately.

### Problem 5: Backend container healthcheck timeout on deploy
- **Type**: template
- **Severity**: minor
- **Backlog**: `done` — added healthcheck with start_period=15s to compose base template (service-template 2bd03ec)
- **Description**: Backend container started successfully but didn't pass healthcheck within the deploy script timeout. Manual restart worked fine.
- **Root cause**: Likely slow DB migration or connection setup on first deploy. Healthcheck interval/retries may be too aggressive.
- **Suggested fix**: Increase healthcheck start_period in compose.prod.yml template.

### Problem 6: Worker environment issues (port conflicts, missing packages)
- **Type**: template
- **Severity**: minor
- **Backlog**: `—`
- **Description**: Workers reported: port 5432 conflict, missing `framework` module, wrong venv shebang paths, missing shared package in service venvs.
- **Root cause**: Worker workspace setup doesn't fully replicate the expected dev environment.
- **Suggested fix**: See individual worker report suggestions.

### Problem 7: TASK.md contains too much duplicated / leaked info
- **Type**: orchestrator
- **Severity**: major
- **Backlog**: `done` — story context now compact list, no descriptions/events/duplication (7f3493c)
- **Description**: TASK.md contains the current task description fully, then repeats it again inside the story context section. The story section also includes full details of future tasks (not yet started), which risks the worker accidentally implementing them. Past worker reports from completed tasks are also included.
- **Root cause**: worker-wrapper writes both task and story context without deduplication or filtering.
- **Suggested fix**:
  - Story section in TASK.md should only list task names (no descriptions/details) with a link to `.story/` directory for more info.
  - Future tasks should show only title with explicit "(do NOT implement — future task)" label.
  - Past worker reports should not be in TASK.md at all — they're in `.story/` if needed.

### Problem 8: AGENTS.md not referenced in worker prompt
- **Type**: orchestrator
- **Severity**: major
- **Backlog**: `done` — worker prompt now references AGENTS.md (6d9eac8)
- **Description**: Claude Code does not read AGENTS.md by default. The worker-wrapper passes `-p 'Read TASK.md and complete the task described there.'` but never mentions AGENTS.md. This means the worker misses framework-specific instructions (e.g., how to use `make generate-from-spec`, template patterns, pre-push hooks).
- **Root cause**: Worker prompt only references TASK.md.
- **Suggested fix**: Change prompt to: `'Read TASK.md and AGENTS.md, then complete the task described in TASK.md.'`

### Problem 9: Worker didn't use template framework capabilities → CI failure
- **Type**: orchestrator
- **Severity**: major
- **Backlog**: `done` — addressed by Problem 8 fix (AGENTS.md in prompt)
- **Description**: First CI failure was due to `protocols.py` not matching expected generated output. Worker manually edited generated files instead of running `make generate-from-spec`. This is likely because AGENTS.md (which documents the framework workflow) was never read (see Problem 8). A separate fix task + worker run was needed to correct this.
- **Root cause**: Worker didn't know about the spec-first workflow because AGENTS.md wasn't in its prompt.
- **Suggested fix**: Reference AGENTS.md in prompt (Problem 8). Additionally, consider adding generated file validation to pre-push hook so the worker catches drift before pushing.

### Problem 10: Worker admin panel shows TASK.md copy instead of raw prompt
- **Type**: orchestrator
- **Severity**: minor
- **Backlog**: `done` — Prompts tab removed entirely; `-p` is now a hardcoded constant, no value in displaying it
- **Description**: The admin panel's prompt history for each worker should show the exact `-p` argument passed to Claude. Currently it shows a copy of TASK.md contents or similar, making it harder to debug what the worker actually received.
- **Root cause**: Prompt history storage captures the wrong data.
- **Resolution**: Removed Prompts tab from admin panel, prompt/prompt-history API endpoints, and all Redis persistence of task_md/prompt_history. The `-p` argument is now always `"Read TASK.md and AGENTS.md, then complete the task described in TASK.md."` — no need to track it.
