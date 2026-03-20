# E2E Report: weather_bot — PASS (with QA fix cycle)

> **Date**: 2026-03-20
> **Project**: weather_bot (project_id: `3eb362a2-01e3-424e-b290-a50208d1b0ec`)
> **Story**: story-85bbd9a2
> **Status**: Passed
> **Feature phase**: skipped
> **Smoke**: pass (health, /api/weather/Moscow both OK)
> **Worker reports**: collected (6)
> **QA report**: collected (2 rounds)

---

## Timeline

```
02:40:01  PO request sent (weather_bot, backend+tg_bot)
02:40:15  PO response — asks for bot token + access mode
02:40:22  Bot token + "all users" access sent to PO
02:40:35  PO confirmed — project created, dev started
02:40:45  Scaffold complete (DRAFT → ACTIVE, ~30s)
02:41:30  Architect — 4 tasks created
02:41:30  task-f74b3c7c  todo → in_dev (Weather service with mock data and caching logic)
04:49:20  task-f74b3c7c  done (~7 min worker time)
04:49:20  task-1b10a2c9  todo → in_dev (Weather cache data model and migrations)
04:52:50  task-1b10a2c9  done (~3.5 min)
04:53:21  task-c1af9c5e  todo → in_dev (Telegram /weather command handler)
04:57:51  task-c1af9c5e  done (~4.5 min)
04:57:51  task-4e1277cc  todo → in_dev (REST API endpoint)
05:00:21  task-4e1277cc  done (~3 min)
05:00:30  Story → pr_review (PR created, auto-merge enabled)
05:02:23  Story → deploying (PR merged, deploy triggered)
05:04:23  Story → testing (deploy done, QA started)
05:07:39  QA FAILED: 8/11 passed, 3 failed
          - /api/weather/{city} → 404 (mounted at /weather/{city} without /api prefix)
          - Field names: temperature_celsius/humidity_percent/condition vs spec temperature/humidity/description
05:08:09  Story → in_progress (fix task created)
05:08:09  task-3c20965a  in_dev (QA fix round 1)
~05:16    task-3c20965a  done → PR review → deploy → QA round 2
~05:22    QA round 2 FAILED again (same path issue persisted?)
~05:22    task-083fefaf  in_dev (QA fix round 2)
~05:28    task-083fefaf  done → PR review → deploy → QA round 3
05:32:30  Story → completed (QA passed)
```

**Total duration**: ~52 min (18 min engineering + 24 min QA fix cycles + 10 min deploy/PR overhead)

## PO Interaction

PO asked for bot token and access mode — standard 2-message exchange.
Token validated successfully against Telegram API (`@factory_e2e_test_bot`).
Project name created as `weather-bot` (hyphenated).

## Verification

- **Health**: `GET /health` → `{"status": "ok"}`
- **Weather API**: `GET /api/weather/Moscow` → `{"city":"Moscow","temperature":-18.3,"humidity":65,"description":"Partly cloudy","cached_at":"..."}`
- **CI**: passed on main (`https://github.com/project-factory-organization/weather-bot/actions/runs/23327815272`)
- **Containers**: 4 running (backend healthy, db healthy, redis healthy, tg_bot up), 0 restarts
- **Caching**: confirmed — second request returns same `cached_at` timestamp

## Problems Found

### Problem 1: API path prefix missing on first deploy
- **Type**: template
- **Severity**: major
- **Backlog**: `new`
- **Description**: Weather API endpoint was mounted at `/weather/{city}` instead of `/api/weather/{city}`. The spec said `GET /api/weather/{city}` but the generated router mounted without the `/api` prefix.
- **Root cause**: Likely the engineer implemented the route without the `/api` prefix, or the framework router configuration doesn't auto-add it. Architect's task description said "GET /api/weather/{city}" but the code used `/weather/{city}`.
- **Suggested fix**: Check if service-template framework has a convention for API prefix. If the spec says `/api/weather/{city}`, the generated route should match exactly.

### Problem 2: Response field names didn't match spec
- **Type**: template
- **Severity**: major
- **Backlog**: `new`
- **Description**: First deploy returned `temperature_celsius`, `humidity_percent`, `condition` instead of spec fields `temperature`, `humidity`, `description`.
- **Root cause**: Engineer chose more descriptive field names that diverged from spec. The domain YAML or the engineer's implementation didn't follow the exact field names from the story.
- **Suggested fix**: Spec field names should be enforced by the architect's task descriptions or by the domain YAML schema.

### Problem 3: 2 QA fix rounds needed
- **Type**: orchestrator
- **Severity**: minor
- **Backlog**: —
- **Description**: First QA fix task (task-3c20965a) didn't fully resolve both issues, requiring a second fix task (task-083fefaf). This extended the pipeline by ~15 min.
- **Root cause**: The first fix worker may have only addressed one of the two issues, or its fix for the path prefix was incomplete.
- **Suggested fix**: Informational — QA fix loop working as designed. The fact that it converged on round 3 is acceptable.
