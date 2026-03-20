# E2E Report: weather_bot — partial pass, QA parsing blocked fix redeploy

> **Date**: 2026-03-20
> **Project**: weather_bot (project_id: `6b725ae7-ac0f-4b48-b4fa-8b5cb6f9fedb`)
> **Story**: story-00c449cc
> **Status**: Failed (QA loop stuck)
> **Feature phase**: skipped
> **Smoke**: pass (health OK, /weather/{city} works, containers healthy)
> **Worker reports**: collected (5)
> **QA report**: collected (10/12 checks passed)

---

## Timeline

| Time  | Event |
|-------|-------|
| 00:04 | PO created project, validated TG bot token |
| 00:05 | Scaffold complete (DRAFT → ACTIVE, ~15s) |
| 00:06 | Architect created 3 tasks, task-88f3a39b → in_dev |
| 00:11 | Task 1 done (tg bot /weather command, ~5.5 min) |
| 00:12 | Task 2 → in_dev (REST API endpoint) |
| 00:18 | Task 2 done (~6 min), Task 3 → in_dev (cache model) |
| 00:19 | Task 3 done (~1 min). All engineering complete — 13 min total |
| 00:19 | Story → in_progress |
| 00:20 | Story → pr_review, PR #1 created |
| 00:20 | PR #1 merged (auto-merge). E2E runner mistakenly triggered manual deploy (poller would have handled it) |
| 00:23 | Deploy #1 complete (SHA 451e5a53) |
| 00:24 | Story → testing, QA run #1 starts |
| 00:27 | QA #1 result: 5/8 passed. /api/weather/{city} → 404, endpoint at /weather/{city} |
| 00:27 | Fix task created (task-f7ca9a5a) |
| 00:31 | Fix task done, PR #2 merged. Deploy #2 ran but redeployed same SHA |
| 00:32 | QA #2 starts |
| 00:38 | QA #2 parsing error: Claude Code `--output-format json` envelope not stripped |
| 00:38 | task-45532c9a → waiting_human_review. Pipeline stuck |

**Total duration**: ~34 min (13 min engineering + 4 min deploy + 17 min QA cycles)

## PO Interaction

PO created the project correctly, validated the Telegram bot token (`@factory_e2e_test_bot`),
set up public access. Two messages: initial request + token. Clean interaction.

## Verification Results

- **Health**: `GET /health` → 200 `{"status": "ok"}`
- **Weather endpoint**: `GET /weather/Moscow` → 200 with city, temperature, condition, humidity, wind_speed, cached fields
- **API weather endpoint**: `GET /api/weather/Moscow` → 404 (fix merged but not redeployed)
- **Caching**: Same temperature on repeated calls, `cached: true`
- **Containers**: backend (healthy), db (healthy), redis (healthy), tg_bot (running)
- **Restart counts**: 0 across all containers

## Problems Found

### Problem 1: E2E skill still references webhooks (removed in b6b7310f)
- **Type**: meta
- **Severity**: minor
- **Backlog**: —
- **Description**: E2E skill instructions reference "webhook may not fire" and include a manual
  deploy trigger recipe. Webhooks were replaced with `poll_merged_prs()` poller on 2026-03-18.
  E2E runner (me) followed stale instructions and manually triggered deploy, potentially
  interfering with the normal poller flow.
- **Root cause**: Skill not updated after webhook→polling migration.
- **Suggested fix**: Remove webhook references from e2e-run skill, remove manual deploy trigger
  recipe, trust the poller.

### Problem 2: Deploy did not pick up fix commit
- **Type**: orchestrator
- **Severity**: major
- **Backlog**: new
- **Description**: Fix task (task-f7ca9a5a) merged PR #2 with commit d335d5c2 but deploy #2
  still deployed SHA 451e5a53. CI passed on the fix but deploy workflow only ran for old SHA.
- **Root cause**: Deploy workflow (deploy.yml) only ran once on the first merge. The second
  merge (fix PR) triggered CI but not deploy. The deploy-worker may have used the cached
  deploy workflow run instead of waiting for a new one.
- **Suggested fix**: Deploy-worker should verify the deployed SHA matches the latest main HEAD,
  or re-trigger deploy workflow when SHA mismatch is detected.

### Problem 3: QA output parsing fails with --output-format json
- **Type**: orchestrator
- **Severity**: major
- **Backlog**: new
- **Description**: QA run #2 failed to parse Claude Code output. The `--output-format json`
  flag wraps output in `{"type":"result","subtype":"success",...,"result":"..."}` envelope.
  QA worker expects raw JSON `{"pass":true/false,"checks":[...]}`.
- **Root cause**: QA runner uses `--output-format json` but doesn't extract `.result` from
  the envelope before attempting to parse the QA JSON.
- **Suggested fix**: In QA runner, parse the outer JSON envelope first, extract `.result`,
  then parse that as the QA result JSON.

### ~~Problem 4~~: Endpoint path mismatch — NOT a problem
- Removed: QA correctly caught /weather vs /api/weather mismatch, fix task was created and
  completed. This is the pipeline working as designed. The only issue is that the fix didn't
  get redeployed (see Problem 2).
