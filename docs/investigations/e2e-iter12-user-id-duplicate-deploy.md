# E2E Investigation: Iteration 12 — User ID + Duplicate Deploy

> **Date**: 2026-02-19
> **Project**: reverse-text-bot (project_id: `15c41942`)
> **Branch**: feat/deploy-architecture
> **Status**: Bugs fixed

---

## Timeline

```
20:12:xx — Services started (make up)
20:13:28 — User message → PO → project 15c41942 (reverse-text-bot)
20:13:36 — Engineering triggered, scaffolding + resource allocation
20:13:48 — Scaffolder complete (repo created, secrets set)
20:14:29 — Worker created, Claude Code started
20:18:58 — Claude Code finished (~4.5 min)
20:19:29 — CI in_progress
20:21:19 — CI passed (lint-and-test + build-and-push both green)
20:21:19 — Deploy auto-triggered by engineering worker
20:21:32 — 9 secrets configured on target server
20:21:34 — deploy.yml dispatched
20:23:25 — deploy.yml completed SUCCESS
20:23:xx — All 4 containers running on target server (backend, tg_bot, db, redis)
20:26:xx — PO reminder fired → PO called trigger_deploy() → DUPLICATE deploy
```

**Total time**: ~11 min (provisioning → deployed). Full pipeline worked end-to-end!
**First successful E2E with complete deploy pipeline.**

---

## BUG 17: proactive_message_send_failed (user_id=unknown) — FIXED

### Description

After deploy completed, telegram bot couldn't send success notification to the user.
`proactive_message_send_failed` with `user_id=unknown`.

### Root Cause

`_handle_engineering_success()` in `engineering_worker.py` created `DeployMessage`
WITHOUT passing the `user_id` parameter:

```python
# BEFORE (bug)
deploy_msg = DeployMessage(
    task_id=deploy_task_id,
    project_id=project_id,
    callback_stream=callback_stream,
    triggered_by=DeployTrigger.ENGINEERING,
)
```

`DeployMessage.user_id` defaults to `""`. Deploy worker received empty user_id →
callback events had no user_id → PO consumer defaulted to `"unknown"` →
telegram bot failed on `int("unknown")`.

### Fix

Added `user_id=user_id` to DeployMessage constructor at `engineering_worker.py:754`:

```python
deploy_msg = DeployMessage(
    task_id=deploy_task_id,
    project_id=project_id,
    user_id=user_id,            # ← ADDED
    callback_stream=callback_stream,
    triggered_by=DeployTrigger.ENGINEERING,
)
```

### Test

`test_deploy_message_includes_user_id` in `test_engineering_worker.py` —
verifies DeployMessage xadd contains the correct user_id.

---

## BUG 18: PO Triggers Duplicate Deploy — FIXED

### Description

PO agent received a reminder, checked task status, saw engineering completed,
and called `trigger_deploy()` — not knowing that deploy was already auto-triggered
by the engineering worker.

Two deploys ran for the same project:
1. Engineering-triggered (20:21) — `triggered_by=engineering`
2. PO-triggered (20:26) — `triggered_by=po`

### Root Cause

PO system prompt was written before auto-deploy existed:
- Example at line 90 said: `completed "Engineering task completed, CI passed" → "Код готов! Начинаю деплой."` — literally telling PO to "start deploy"
- No mention that deploy is automatic after engineering
- `trigger_deploy` scenario didn't clarify it's only for manual re-deploys

### Fix

Updated PO system prompt (`services/langgraph/src/po/prompts.py`):

1. Added "Automatic Deploy Pipeline" section explaining auto-deploy flow
2. Changed "REDEPLOY" scenario to clarify: only for explicit user re-deploy requests
3. Updated example: `completed "Engineering task completed, CI passed" → "" (stay silent)`
4. Added rule #5: "NEVER call trigger_deploy() after engineering tasks"

### Defense in Depth

Deploy worker already has `_check_duplicate_deploy()` guard that checks for
RUNNING/QUEUED deploy tasks before starting. Even if PO calls `trigger_deploy()`,
the second deploy is caught and skipped with `deploy_skipped_duplicate` log.

---

## What Worked

1. **Full pipeline E2E** — first successful end-to-end from user message to deployed project
2. **Registry TLS** — Caddy certificate valid, CI build-and-push through registry worked
3. **CI fix classification (BUG 15)** — not tested (CI passed on first try)
4. **Dedup guard (BUG 13)** — caught the PO's duplicate deploy
5. **All BUG 7-16 fixes** — no regressions observed
6. **4 containers on target server** — backend, tg_bot, db, redis all running
