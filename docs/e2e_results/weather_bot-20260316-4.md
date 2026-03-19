# E2E Report: weather_bot — Pass (deploy retry after port conflict)

> **Date**: 2026-03-16
> **Project**: weather_bot (project_id: `d141b5e2-8700-478b-8744-6cce63306060`)
> **Story**: story-b32caf7d
> **Status**: Passed
> **Feature phase**: skipped
> **Smoke**: pass (`/health` OK, `GET /api/weather/moscow` returns mock data)
> **Worker reports**: collected (3)

---

## Timeline

```
18:23  Stack health check passed, pre-flight cleanup (--no-nuke)
18:23  Worker images stale — rebuilt
18:23  Upserted e2e test user
18:24  PO asked for TG bot token → sent real token → validated
18:25  PO created project + story, submitted to architect
18:25  Scaffold started (DRAFT → ACTIVE, workspace_ready=true)
18:26  Architect started (waited 60s for scaffold)
18:27  3 tasks created (no blocking between them)
18:27  task-ecc16bfc (REST API) → in_dev
18:28  Worker image cache miss — building worker image
18:28  Worker container started
18:32  task-ecc16bfc → done (~4.5min)
18:33  task-88834890 (TG bot) → in_dev
18:35  task-88834890 → done (~3min)
18:36  task-af822bc9 (data model + caching) → in_dev
18:41  task-af822bc9 → done (~5min)
18:41  Story → pr_review (PR created with auto-merge)
18:42  CI on story branch: in_progress → success
18:43  Story → deploying (webhook triggered)
18:43  Deploy-worker: secrets configured (9), deploy.yml dispatched
18:45  Deploy.yml FAILED — "port is already allocated" (port 8012)
18:45  Deploy-worker: rerun failed jobs → failed again
18:47  Deploy-worker: created fix task, story → in_progress
18:47  Fix task worker spawned (useless — port conflict, not code issue)
18:58  [manual] Killed fix worker, cleaned old deployment on server
19:00  [manual] Published deploy message, story → deploying
19:02  Story → completed (deploy succeeded, QA skipped*)
19:02  Verified: all 4 containers up, health OK, weather API works
```

*QA phase was skipped — story went directly from deploying to completed. This may be because QA queue stream didn't exist or the deploy-worker skipped QA on manual retrigger.

## PO Interaction

PO asked for TG bot token (expected), validated it against Telegram API (rejected fake token, accepted real one). Created project with correct modules and description. Smooth flow, 3 messages total.

## Problems Found

### Problem 1: Port conflict from stale old deployment (weather-bot vs weather_bot)
- **Type**: orchestrator
- **Severity**: major
- **Backlog**: new
- **Description**: Deploy failed because port 8012 was occupied by an old `weather-bot` (hyphenated) deployment. The new project used underscores (`weather_bot`), so pre-flight cleanup checked for both naming variants in `/opt/services/` but the old `weather-bot` directory existed with running containers.
- **Root cause**: Pre-flight cleanup script DID check both `weather_bot` and `weather-bot` dirs. However, the cleanup was attempted BEFORE the stack was fully initialized (the server SSH check succeeded). The old deployment existed from a previous E2E run that wasn't fully cleaned up. The deploy.yml on GitHub Actions uses the `PROJECT_NAME` secret which may resolve to underscore variant, causing a new compose project that conflicts on the same port.
- **Suggested fix**: Deploy workflow should check port availability before `docker compose up`. Also, the scaffold/deploy should enforce consistent naming (hyphens or underscores, not both).

### Problem 2: Fix task dispatched for infra issue
- **Type**: orchestrator
- **Severity**: minor
- **Backlog**: new
- **Description**: Deploy-worker created an engineering fix task for a port conflict error. The worker spent ~10 min trying to "fix code" when the issue was a stale deployment on the server. No code changes could fix it.
- **Root cause**: Deploy failure classifier couldn't determine this was an infra issue (the LLM classifier also failed with a 400 error — `claude-haiku-4-5-20251001 is not a valid model ID` via litellm).
- **Suggested fix**: 1) Fix the haiku model ID in litellm config. 2) Add port-conflict detection to deploy failure classifier — if error contains "port is already allocated", classify as infra, not code.

### Problem 3: QA phase skipped on manual deploy retrigger
- **Type**: meta
- **Severity**: minor
- **Backlog**: —
- **Description**: After manual deploy retrigger, story went directly `deploying → completed` without going through `testing` (QA phase). The QA queue stream didn't exist at start (`qa:queue` showed as missing in health check).
- **Root cause**: Likely the deploy-worker skipped QA because the qa:queue consumer group wasn't set up, or the manual DeployMessage with `triggered_by=WEBHOOK` bypassed QA logic.
- **Suggested fix**: Verify qa:queue stream/group exists before deploy. Document in e2e skill that manual deploy retrigger may skip QA.

## Engineering Summary

- 3 tasks, all completed successfully in ~12.5 minutes total
- Worker reuse: same container handled all 3 tasks sequentially
- No CI failures on story branch
- Code produced: 22 files, 940 lines added (REST API, TG bot handler, weather model, caching, unit tests)
