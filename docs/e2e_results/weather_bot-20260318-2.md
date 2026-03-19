# E2E Report: weather_bot — deploy smoke failed (tg_bot), backend OK

> **Date**: 2026-03-18
> **Project**: weather-bot (project_id: `0055c861-67de-49fe-abb5-55f74c434b6d`)
> **Story**: story-f32fcf6c
> **Status**: Failed
> **Feature phase**: skipped
> **Smoke**: backend pass, tg_bot fail
> **Worker reports**: collected (3)

---

## Timeline

```
14:11:26  Test user created (id=40)
14:11:44  PO message sent (create weather_bot)
14:12:05  PO asked for bot token + access preference
14:12:10  Follow-up sent (skip token, public access)
14:12:14  PO created project "weather-bot" (draft)
14:12:14  Repository record created (repo-430b31ec)
14:12:41  Scaffold completed (project: draft → active, workspace_ready=true)
14:12:42  TG_BOT_TOKEN injected into project secrets
14:13:51  Architect completed — 3 tasks created (no blocking deps)
14:14:41  task-916d91d7 "Implement Telegram bot /weather command" → in_dev
14:22:10  task-916d91d7 → done (~8 min)
14:25:11  task-8fe52f4b "Implement REST API endpoint" → done
14:25:41  task-f2f2496f "Create weather cache database model" → in_dev
14:29:12  task-f2f2496f → done — all 3 tasks complete (~15 min engineering)
14:30:42  Story status: deploying (PR merged, deploy triggered)
14:30:56  Deploy secrets configured (9 secrets)
14:30:58  deploy.yml workflow dispatched
14:32:19  GH Actions deploy completed successfully (run_id: 23244780694)
14:32:19  Deployment record created
14:32:19  Smoke test — backend: pass (HTTP 200)
14:32:19  Smoke test — tg_bot: FAIL ("attempt to write a readonly database")
14:32:22  Deploy failure classified as GIVE_UP
14:32:22  Story → failed
```

## PO Interaction

PO asked clarification questions about bot token and access level. On follow-up,
created the project correctly with all modules. Good behavior.

## Problems Found

### Problem 1: Smoke test tg_bot — readonly database error

- **Type**: orchestrator
- **Severity**: major
- **Backlog**: new
- **Description**: Deploy worker's tg_bot smoke check failed with `OperationalError: attempt to write a readonly database`. This is a Telethon session SQLite error — the deploy-worker uses Telethon to verify the bot is responding, but the SQLite session file is read-only inside the container.
- **Root cause**: The deploy-worker container likely has a Telethon `.session` file that was created on a read-only filesystem or with wrong permissions. The smoke test tries to write to it.
- **Suggested fix**: Either mount the session storage as a writable volume, or switch the tg_bot smoke check to use the Bot API directly (e.g., `getMe` endpoint) instead of Telethon client login.

### Problem 2: TG bot 409 Conflict on production server

- **Type**: other (shared bot token)
- **Severity**: minor
- **Backlog**: —
- **Description**: The deployed tg_bot container gets `409 Conflict: terminated by other getUpdates request` continuously. This means another bot instance is polling the same token.
- **Root cause**: The same `TELEGRAM_BOT_TOKEN` is likely used by another running bot instance (previous deploy, dev environment, or the smoke test itself left a dangling connection).
- **Suggested fix**: Ensure the e2e test uses a dedicated bot token not used elsewhere. Also, the smoke test's Telethon client may be calling `getUpdates` which conflicts — ensure it cleanly disconnects.

### Problem 3: Deploy classified as GIVE_UP too aggressively

- **Type**: orchestrator
- **Severity**: minor
- **Backlog**: new
- **Description**: The deploy workflow succeeded (GH Actions green, backend healthy, all containers running). But because the tg_bot smoke failed, the entire deploy was classified as GIVE_UP and the story was marked failed. The backend is perfectly functional at `http://80.209.235.229:8012`.
- **Root cause**: The failure classifier doesn't distinguish between "deploy infrastructure failed" and "smoke test for one module failed". A partial success should be reported differently.
- **Suggested fix**: Consider a "partial_success" classification — mark the story as needing attention rather than failed when backend passes but a secondary module's smoke fails.

## Verification

Despite the "failed" status, the deployment is live and working:
- Backend health: `GET http://80.209.235.229:8012/health` → `{"status": "ok"}`
- Weather API: `GET http://80.209.235.229:8012/api/weather/moscow` → `{"temperature": 16, "description": "Overcast", "humidity": 83, "wind_speed": 6.4}`
- All 4 containers running (backend, db, redis, tg_bot) with 0 restarts
