# E2E Report: weather_bot — partial pass (backend OK, tg_bot crash loop)

> **Date**: 2026-03-18
> **Project**: weather-bot (project_id: `a5f77e2c-b34d-4e5c-88d1-f72dd64930fd`)
> **Story**: story-98c0badb
> **Status**: Failed (deploy)
> **Feature phase**: skipped
> **Smoke**: backend pass / tg_bot fail
> **Worker reports**: collected (3)

---

## Timeline

```
09:00  Pre-flight cleanup, worker images rebuilt (stale hash)
09:01  PO request sent (weather_bot, backend+tg_bot)
09:01  PO asked for bot token + access policy → replied "public, token later"
09:01  PO created project weather-bot + story-98c0badb
09:01  Scaffold triggered, architect started (waited 40s for scaffold)
09:02  Architect created 3 tasks (no blocking dependencies)
09:02  Architect failed on story.start (422 — story already in_progress). Non-critical.
09:02  Telegram bot token injected into project secrets
09:03  task-c62ead88 (Backend: Weather REST API endpoint) → in_dev
09:10  task-c62ead88 → done
09:10  task-eb805cf2 (Telegram bot: /weather command) → in_dev
09:13  task-eb805cf2 → done
09:13  task-e105a5a6 (Backend: Weather data model and caching) → in_dev
09:14  task-e105a5a6 briefly → todo (retry?), then back → in_dev
09:18  task-e105a5a6 → done. All tasks complete.
09:19  PR #1 created, auto-merge enabled → PR merged
09:20  Story → deploying (scheduler/webhook triggered deploy)
09:22  Deploy workflow failed: "Deploy via SSH" step — tg_bot crash loop (15 restarts)
09:23  Deploy-worker retry 1/3 → re-ran failed jobs → failed again
09:26  Manual deploy trigger (deploy-e2e-8dd7bbde)
09:28  Deploy failed again (attempt 2/3) — same tg_bot crash loop
09:29  Story rolled back to in_progress
09:30  Investigation: tg_bot ImportError confirmed
```

## PO Interaction

PO correctly created the project, asked for bot token and access policy. Responded well to follow-up. Story and project created on second message. One minor issue: architect tried to `start` story that was already `in_progress` (422) — non-critical, tasks were created successfully.

## Verification

- **Backend health**: `GET /health` → `{"status": "ok"}` ✅
- **Weather API**: `GET /api/weather/moscow` → `{"city": "moscow", "temperature": 21.0, "description": "Rain", "humidity": 74}` ✅
- **tg_bot**: crash loop ❌

## Problems Found

### Problem 1: tg_bot crash loop — relative import in main.py
- **Type**: template
- **Severity**: critical
- **Backlog**: new (template issue)
- **Description**: Generated tg_bot service crashes on startup with `ImportError: attempted relative import with no known parent package` at `from .middleware import install_update_logging` in `services/tg_bot/src/main.py` (line 24).
- **Root cause**: `main.py` is executed as a script (`python main.py`) but uses relative imports (`.middleware`). Relative imports only work when the file is part of a package (run via `python -m`).
- **Suggested fix**: Either change the Dockerfile entrypoint to `python -m services.tg_bot.src.main` or change relative imports to absolute imports in the template's tg_bot generator.

### Problem 2: Architect 422 on story.start (minor)
- **Type**: orchestrator
- **Severity**: minor
- **Backlog**: —
- **Description**: Architect tried to transition story to `start` but PO had already transitioned it to `in_progress` when submitting to architect:queue. Result: 422 error, architect job marked as failed even though all 3 tasks were created successfully.
- **Root cause**: Race condition — PO calls `create_story` which transitions story to `in_progress`, then architect also tries `start`.
- **Suggested fix**: Architect should catch 422 on `start` and treat it as a no-op if tasks were already created.

### Problem 3: Deploy failure classifier cannot classify
- **Type**: orchestrator
- **Severity**: minor
- **Backlog**: —
- **Description**: `deploy_classify_unexpected` — the LLM classifier returned "I CANNOT CLASSIFY THIS FAILURE" instead of a valid category. This caused the deploy-worker to do a generic rollback instead of a targeted retry.
- **Root cause**: The classifier LLM didn't have enough context from the GH Actions error (just "failure" status, no log content).
- **Suggested fix**: Pass the actual job step failure logs to the classifier, not just the status.
