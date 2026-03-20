# E2E Report: weather_bot — pass (with QA fix cycle)

> **Date**: 2026-03-20
> **Project**: weather_bot (project_id: `5e356231-d767-45aa-94ea-ddcedf009763`)
> **Story**: story-129fe849
> **Status**: Passed
> **Smoke**: pass (2/2 — backend HTTP 200, tg_bot responded)
> **Worker reports**: collected (3)
> **QA report**: collected (10/10 checks passed)

---

## Timeline

```
14:57  PO: project created (weather-bot), asked for TG token
14:57  PO: token validated, asked about access
14:57  PO: story created, pipeline started
14:58  Scaffold complete (draft → active, ~15s)
14:58  Architect created 3 tasks
14:59  Task 1 (weather cache model) → in_dev
15:06  Task 1 → done (~7 min)
15:06  Task 2 (/weather command) → in_dev
15:09  Task 2 → done (~3.5 min)
15:10  Task 3 (API endpoint) → in_dev
15:13  Task 3 → done (~3 min)
15:13  Story → pr_review (PR created, auto-merge enabled)
15:15  PR merged, deploy triggered
15:16  Deploy completed (GH Actions), smoke passed (2/2)
15:17  QA: SSH connected, Claude Code OAuth 400 → QA failed (refresh token expired)
15:17  QA fix task created — worker correctly identified infra issue
15:20  Manual intervention: marked fix task done, story re-entered deploy cycle
15:23  Second QA attempt — same OAuth failure
15:31  Cancelled second fix task, manually completed story
------- credential refresh fix implemented -------
14:35  Credential refresh loop started, pushed local creds to server (fallback)
14:37  Story reopened, deploy re-triggered
14:39  Deploy completed, QA started with fresh credentials (TTL=8328s)
14:45  QA passed: 10/10 checks
14:45  Story → completed
```

**Total duration**: ~48 min including credential fix implementation

## PO Interaction

PO created the project correctly, asked for TG bot token and access level.
3 messages exchanged total. Clean flow, no issues.

## Problems Found & Fixed

### Problem 1: QA Claude Code OAuth expired on server (FIXED)
- **Type**: other (infra) → orchestrator fix
- **Severity**: major → resolved
- **Description**: Refresh token expired between QA runs (~10h gap). The reactive-only
  refresh in `_ensure_claude_credentials` couldn't recover because the refresh token
  itself was dead (400 from OAuth endpoint).
- **Root cause**: Credentials only refreshed when QA runs. If no QA job arrives for
  longer than the refresh token lifetime, the token expires irrecoverably.
- **Fix applied**:
  1. **Periodic refresh loop** — `credential_refresh_loop()` runs every 4h in qa-worker,
     SSHes to all managed servers and refreshes credentials proactively.
  2. **Local fallback** — when refresh returns 400/401, reads fresh credentials from
     `~/.claude/.credentials.json` (mounted at `/secrets/claude-credentials.json`)
     and pushes them to the server.

### Problem 2: QA fix cycle loops on infra issues
- **Type**: orchestrator
- **Severity**: minor
- **Backlog**: new
- **Description**: When QA fails due to infra (not code), the system creates a "QA fix" task
  and sends it to a coding worker. The worker correctly identifies "not a code issue" and gives
  up with `waiting_human_review`. But when manually resolved, the cycle repeats.
- **Root cause**: QA failure handler always creates a fix task regardless of failure type.
- **Suggested fix**: Parse worker's `failure_metadata` for "NOT A CODE ISSUE" pattern.

## Deployment Verification

- **CI**: passed (main branch)
- **Containers**: 4/4 healthy (backend, db, redis, tg_bot), 0 restarts
- **Health endpoint**: `GET /health` → `{"status": "ok"}`
- **Weather API**: `GET /api/weather/tokyo` → valid JSON with city, temperature, description, humidity, wind_speed, cached_at
- **TG Bot**: responded to smoke test ("Привет!")
- **QA**: 10/10 checks passed (REST API, caching, Telegram bot, container health, shared cache)
- **Deploy URL**: http://80.209.235.229:8012
