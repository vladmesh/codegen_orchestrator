# E2E Report: fortune-teller-bot (Feature Enhancement)

**Date**: 2026-03-11
**Project**: fortune-teller-bot (e0b64cd2-1673-4b40-871a-f78ce4925f9b)
**Story**: story-3844bb0b — "Make LessWrong articles relevant to user's theme"
**User**: 93459832 (Telegram)
**Modules**: backend, tg_bot
**Previous story**: story-c5f4cd46 (initial bot creation, deployed at 18:53)

## Timeline

| Time (UTC) | Event |
|---|---|
| 19:47 | User requests feature: make LessWrong articles relevant to prediction theme |
| 19:47 | PO creates story-3844bb0b, submits to architect (action=feature) |
| 19:48 | Architect creates 1 feature task + 1 CI-check task |
| 19:48 | Task 1 dispatched to engineering worker |
| 19:48 | Worker container created (`dev-fortune-teller-bot-9ecad5ce`) |
| 19:56 | Task 1 done — commit `465f2d3` (LessWrong AI selection + integration) |
| 19:56 | Task 2 (CI-check) dispatched, reusing same worker container |
| 20:01 | CI-check agent finds test issue, commits fix `f214db1` (conftest.py) |
| 20:09 | CI-check agent finds lint issue, commits fix `538410b` (shared module path) |
| 20:10 | Agent pushes all 3 commits to GitHub, CI starts |
| 20:11 | CI passed (run 22972239326) |
| 20:12 | Engineering worker detects success, CI gate confirms pass |
| 20:12 | Both tasks marked done, worker container cleaned up |
| 20:13 | Dispatcher completes story, triggers deploy with `action=create` |
| 20:13 | **Deploy FAILS** — "Service dir already exists" (project was already deployed) |
| 20:13 | User receives 1st batch of technical spam (deploy status messages) |
| ~20:14 | **Manual intervention**: set project status `developing→active`, trigger deploy `action=feature` |
| 20:15 | Deploy workflow starts on GitHub |
| 20:17 | **Deploy SUCCESS** — smoke pass (backend health: HTTP 200, tg_bot: skip) |
| 20:17 | Story completed, project status active |
| 20:21 | PO reminder fires, sees story completed, sends user completion message (254 chars) |

## What Worked

1. **Feature task executed perfectly** — single task, 8 min, correct implementation
2. **CI-check task actually found and fixed real issues** — test conftest missing + lint errors (2 fix commits). This validates CI-check tasks when there's something to fix.
3. **Worker container reuse** — same container used for both tasks (story worker registry)
4. **CI gate** — engineering worker correctly waited for GitHub CI and confirmed pass
5. **Deploy (after manual fix)** — `action=feature` deployed cleanly with smoke test pass
6. **PO completion notification** — reminder-based check found story completed, sent user a proper 254-char message

## Problems Found

### 1. Project status `developing` after first successful deploy
- **Severity**: critical
- **Type**: orchestrator (data)
- **Backlog**: existing (see initial report #4/#9)
- **Status**: ⬚ TODO

After the initial story's deploy was manually completed, `project.status` remained `developing` instead of being set to `active`. This caused the dispatcher to use `action=create` for the feature deploy, which failed with "Service dir already exists".

**Root cause**: The initial deploy was fixed manually (direct DB/API status changes), but `project.status` was not updated to `active`. The deploy worker sets project status to `active` on success, but the manual recovery path skipped this.

**Impact**: Every subsequent deploy for this project will fail until project status is manually fixed.

**Fix**: Two options:
1. Dispatcher should check server for existing dir (`test -d`) rather than relying solely on `project.status` to determine deploy action
2. Deploy precheck failure for "dir exists" should auto-retry with `action=feature` instead of failing

### 2. Technical spam to user — 11+ proactive messages
- **Severity**: major
- **Type**: orchestrator
- **Backlog**: existing (initial report #5)
- **Status**: ⬚ TODO — same problem, now worse

User received **11 proactive messages** during the deploy cycle:
- Multiple "tasks done, deploy triggered" messages
- Multiple "deploy precheck failed: dir exists" messages
- Multiple "deploy success" messages (from two parallel deploy runs)

Same root cause as initial report: raw deploy status published to `po:proactive` stream → forwarded to user verbatim.

### 3. Story transition race condition on completion
- **Severity**: minor
- **Type**: orchestrator
- **Backlog**: new
- **Status**: ⬚ TODO

Deploy worker got 422 when trying `story.complete()` because the story was already completed. Sequence:
1. Deploy precheck fails → deploy worker calls `story.start()` (deploying→in_progress)
2. Dispatcher sees all tasks done, completes story again, triggers new deploy
3. Manual deploy succeeds → tries `story.complete()` → 422 (already completed by dispatcher)

The `_transition_story_safe` catches this gracefully (logged as warning, no crash), but it reveals a coordination gap between dispatcher and deploy worker.

### 4. Old story stuck in `deploying` — PO wastes API calls polling
- **Severity**: minor
- **Type**: orchestrator (data)
- **Backlog**: —
- **Status**: ✅ FIXED (manual: story-c5f4cd46 `deploying→completed`)

story-c5f4cd46 was stuck in `deploying` from the initial run. PO kept firing reminders every 20 min to check it ("deploying for almost/over an hour"). Wasted API calls and cluttered PO context.

### 5. Minor Arcana tarot images broken — `sendPhoto 400`
- **Severity**: minor
- **Type**: generated code (pre-existing)
- **Backlog**: —
- **Status**: ⬚ known, not caused by this feature

All Minor Arcana cards (56 of 78) use hardcoded Wikimedia thumb URLs with an incorrect path hash (`/thumb/8/8e/RWS_Tarot_{suit}{card}.jpg/300px-...`). The `8/8e` hash is the same for every card and doesn't correspond to actual files. Wikimedia returns 429/404, Telegram gets the error and returns `sendPhoto 400 Bad Request`.

Major Arcana (22 cards) use direct commons URLs that work fine.

This is a bug in the original `tarot.py` from the initial story, not introduced by this feature (diff confirms `tarot.py` was not modified).

**Fix**: Either use direct commons URLs for all cards (like Major Arcana does), or embed small tarot images in the repo.

### 6. Two parallel deploy workflow runs
- **Severity**: minor
- **Type**: orchestrator
- **Backlog**: new
- **Status**: ⬚ TODO

Two successful `deploy.yml` runs (22972442025, 22972507409) ran nearly simultaneously on the same commit `538410ba`. One was from the dispatcher's auto-retry (which used `action=create` and failed precheck — but the GitHub Actions workflow was already triggered), the other from the manual `action=feature` deploy.

The deploy worker correctly handled this — both completed successfully — but it wastes GitHub Actions minutes and could cause race conditions on the server.

## Summary

- **Feature task**: 1/1 completed by automation in 8 min
- **CI-check task**: Completed successfully — agent found and fixed 2 real issues (conftest.py + lint)
- **Deploy (auto)**: Failed — `action=create` on existing deployment (project status bug)
- **Deploy (manual fix)**: Succeeded after setting project status to `active`
- **PO notification**: User received completion message via reminder (~4 min after deploy)
- **Total time**: ~34 min (19:47 → 20:21) — 24 min automated, 10 min manual intervention
- **Manual interventions**: 3 (fix old story, fix project status, trigger deploy with correct action)

## Recommendations

### From initial report — still open:
3. ✅ **HIGH**: CI-check task no longer fails on "no commit" — `allow_no_commit` flag allows verification-only success
5. ✅ **HIGH**: `shared/generated/events.py` now always generated as stub — import never fails (fix in service-template)
7. ✅ **HIGH**: Stop pushing deploy status to `po:proactive` — spam filter implemented. Only deploy success and permanent story failure reach user

### New from this run:
11. ✅ **CRITICAL**: Deploy auto-fallback `create→feature` when precheck fails with "dir exists" (fixed 2026-03-11)
12. ✅ **MEDIUM**: Deploy deduplication — atomic Redis lock (`SET NX`) per project replaces non-atomic DB check. 5 unit tests.
13. ⬚ **LOW**: Story completion race — deploy worker and dispatcher both try to complete story, causing 422. Add idempotency or coordination.
