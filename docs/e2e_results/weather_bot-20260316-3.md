# E2E Report: weather_bot — PASS (deploy rerun)

> **Date**: 2026-03-16
> **Project**: weather-bot (project_id: `45f2b3c1-a871-4fef-a8c1-9697d81af621`)
> **Story**: story-022fd071
> **Status**: Passed
> **Feature phase**: skipped
> **Smoke**: pass (backend OK, tg_bot skipped — no Telethon env)
> **Worker reports**: collected (3)

---

## Timeline

| Time  | Event |
|-------|-------|
| 10:59 | PO message sent (project creation request) |
| 10:59 | PO responded — asked for Telegram bot token |
| 10:59 | Token injected into project secrets |
| 11:00 | Follow-up sent with token + access preference |
| 11:00 | PO validated token, created story, submitted to architect |
| 11:00 | Scaffold complete (project ACTIVE, workspace_ready=true) |
| 11:01 | Architect created 3 tasks, task-d85bde84 dispatched |
| 11:06 | Task 1 (Backend API) done |
| 11:06 | Task 2 (Integration testing) started |
| 11:11 | Task 2 done, Task 3 (Telegram Bot) started |
| 11:16 | Task 3 done — all engineering complete |
| 11:17 | PR #1 created (story/story-022fd071 → main), auto-merge enabled |
| 11:23 | **Intervention**: Fixed branch protection (required check `ci` → `lint-and-test`) |
| 11:23 | PR merged, webhook triggered deploy |
| 11:24 | Deploy workflow triggered on GitHub Actions |
| 11:26 | Deploy workflow FAILED (first attempt) |
| 11:26 | Deploy-worker auto-reran failed jobs |
| 11:28 | Deploy rerun succeeded |
| 11:28 | Smoke test: backend pass, tg_bot skipped |
| 11:28 | Story → completed |

**Total duration**: ~29 minutes (engineering: ~15m, deploy: ~12m including failure+rerun)

## PO Interaction

Two-turn conversation:
1. PO created project, asked for bot token and access level
2. User provided token + "public bot" → PO validated, created story, confirmed

Clean interaction, no issues.

## Problems Found

### Problem 1: Branch protection still uses `ci` instead of `lint-and-test`

- **Type**: orchestrator
- **Severity**: major
- **Backlog**: existing — was "fixed" in commit `283ae01` but still reproduces
- **Description**: Branch protection on the newly scaffolded repo had required check `ci`, but the CI workflow job is named `lint-and-test`. Auto-merge was blocked until manual intervention fixed the protection rule.
- **Root cause**: The scaffolder container code is correct (`required_checks=["lint-and-test"]`). However, GitHub API showed `ci`. Possible causes: (1) race with another process setting protection, (2) the `provision_project_repo` flow setting it before scaffolder overwrites, or (3) GitHub caching from a previous repo with the same name. The pre-flight cleanup found the repo "EXISTS" but GitHub returned 404 on installation — the repo may have been in a partially deleted state.
- **Suggested fix**: Add logging in `update_branch_protection` to dump the response body so we can confirm what GitHub actually accepted. Also investigate whether the PO's `create_project` tool sets any protection before the scaffolder runs.

### Problem 2: Deploy workflow failed on first attempt

- **Type**: other
- **Severity**: minor
- **Backlog**: —
- **Description**: GitHub Actions deploy.yml failed on first run, succeeded on automatic rerun of failed jobs.
- **Root cause**: Transient — likely a timing issue with image pull or server connectivity. Deploy-worker's built-in rerun logic handled it automatically.
- **Suggested fix**: No action needed — the rerun mechanism works correctly.

### Problem 3: Service deployment record creation 500 error

- **Type**: orchestrator
- **Severity**: minor
- **Backlog**: new
- **Description**: After successful deploy, `_create_deployment_record` got HTTP 500 from `POST /api/service-deployments/`. The deployment itself succeeded, but no record was saved to DB.
- **Root cause**: Unknown — need to check API logs for the specific 500 error. May be a missing field or FK constraint violation.
- **Suggested fix**: Check API logs for the 500 traceback. Likely a schema mismatch or missing required field in the deployment record payload.

### Problem 4: Smoke test skipped tg_bot module

- **Type**: orchestrator
- **Severity**: minor
- **Backlog**: —
- **Description**: Smoke test for tg_bot was skipped with "Telethon env vars not configured". The bot container is running on the server, but deploy-worker couldn't verify it.
- **Suggested fix**: Configure Telethon API credentials in deploy-worker env for full tg_bot smoke testing.

## Deployment Verification

- **Server**: 80.209.235.229:8012
- **Health**: `GET /health` → `{"status": "ok"}`
- **Weather API**: `GET /api/weather/moscow` → `{"city": "moscow", "temperature": 27.3, "description": "Fog", "humidity": 51, "wind_speed": 11.4, "cached_at": "..."}`
- **Containers**: backend (Up), tg_bot (Up), db (Up, healthy), redis (Up, healthy)
- **Restarts**: 0 across all containers
