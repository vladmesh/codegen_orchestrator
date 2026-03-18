# E2E Report: weather_bot — Deploy smoke false positive (backend OK, tg_bot actually running)

> **Date**: 2026-03-19
> **Project**: weather_bot (project_id: `6ed593a1-342b-484a-bf8f-b9deb812107a`)
> **Story**: story-2eb52bb9
> **Status**: Failed (false positive — actual deploy succeeded)
> **Smoke**: fail (backend pass, tg_bot false fail)
> **Worker reports**: collected (2)

---

## Timeline

```
23:26  PO: project creation request sent
23:26  PO: project "weather-bot" created, asks for bot token
23:26  PO: token sent, story story-2eb52bb9 created
23:27  Scaffold complete (project: active, workspace_ready: true)
23:27  Architect: 2 tasks created
        task-5878ae48: Implement Telegram bot /weather command
        task-4beefba0: Implement weather API endpoint with PostgreSQL caching
23:28  task-5878ae48 → in_dev (worker started)
23:32  task-5878ae48 → done (~4 min)
23:32  task-4beefba0 → in_dev (second worker started, same container reused)
23:39  task-4beefba0 → done (~7 min)
23:39  Story → pr_review (PR created, auto-merge enabled)
23:40  CI passed on story branch, PR merged, CI passed on main
23:40  First deploy message published (action=feature, wrong action type)
23:40  Story went back to in_progress briefly (webhook race?)
23:42  Second deploy message published (action=create, retry)
23:42  Deploy-worker: env analysis, secret resolution, GitHub secrets configured
23:42  Deploy-worker: deploy.yml workflow triggered
23:43  Deploy completed, containers running on server
23:44  Smoke test: backend PASS, tg_bot FAIL (TelegramClient bug)
23:44  Deploy-worker classified as GIVE_UP → story failed
```

## PO Interaction

Smooth. PO created project, asked for bot token, accepted and stored it. No issues.

## Deployment Verification (manual)

Despite the smoke failure, actual deployment is fully functional:
- Backend: healthy at http://80.209.235.229:8012
- `GET /api/weather/moscow` returns `{"city": "moscow", "temperature": 0.3, "humidity": 62, "description": "Rainy"}`
- tg_bot container: running (Up, no healthcheck defined)
- All 3 containers up: backend (healthy), db (healthy), redis (healthy), tg_bot (running)
- Both `BACKEND_IMAGE` and `TG_BOT_IMAGE` env vars correctly set

## Problems Found

### Problem 1: Smoke test TelegramClient bug
- **Type**: orchestrator
- **Severity**: major
- **Backlog**: new
- **Description**: Smoke test `_check_tg_bot` fails with `'TelegramClient' object has no attribute 'get_response'`. This is a bug in the deploy-worker's Telethon smoke checker, not in the deployed bot. The bot is actually running fine.
- **Root cause**: The `TelegramClient` (Telethon) API call `get_response` doesn't exist. Previous fix `1dea9740` addressed readonly session but this method issue persists.
- **Suggested fix**: Fix the `_check_tg_bot` method in `services/langgraph/src/agents/devops/nodes/smoke.py` to use the correct Telethon API for sending messages and reading responses.

### Problem 2: Double deploy message with wrong action type
- **Type**: orchestrator
- **Severity**: minor
- **Backlog**: new
- **Description**: Two deploy messages were published to `deploy:queue`. The first had `action=feature` (wrong — this is a new project, not a feature add). The second had `action=create` (correct, retry). This suggests the webhook handler or PR poller has a race condition where it fires twice with different action types.
- **Root cause**: Likely the PR merge webhook and the PR poller both detected the merge and published separate deploy messages with different `action` values.
- **Suggested fix**: Deduplicate deploy messages by story_id, or use an idempotency key to prevent double-publish.

### Problem 3: Telegram notification parse error
- **Type**: orchestrator
- **Severity**: minor
- **Backlog**: existing (notification formatting)
- **Description**: Admin notification failed with `Bad Request: can't parse entities: Can't find end of the entity starting at byte offset 322`. The HTML message sent to the admin had malformed entities.
- **Suggested fix**: Sanitize/escape HTML entities in error messages before sending via Telegram.
