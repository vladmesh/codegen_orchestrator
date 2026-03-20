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
| 00:20 | PR #1 merged (auto-merge). Webhook did NOT fire |
| 00:21 | Manual deploy trigger (known webhook issue for new repos) |
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

### Problem 1: Webhook not firing for newly scaffolded repos
- **Type**: orchestrator
- **Severity**: major
- **Backlog**: known issue (documented in skill)
- **Description**: After PR #1 merged, story stayed in pr_review. Webhook never arrived.
- **Root cause**: GitHub App webhook may not be configured for repos created during the run.
- **Suggested fix**: Verify webhook setup after repo creation in scaffolder, or poll for merge.

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

### Problem 4: Endpoint path mismatch — /weather vs /api/weather
- **Type**: template (or engineering agent)
- **Severity**: minor
- **Backlog**: —
- **Description**: Task description said "GET /api/weather/{city}" but engineer implemented
  as "/weather/{city}". Fix task correctly identified and fixed it, but fix wasn't deployed.
- **Root cause**: Architect's acceptance criteria ambiguity or engineer interpretation.
- **Suggested fix**: N/A — the fix was created and merged, just not deployed (see Problem 2).
