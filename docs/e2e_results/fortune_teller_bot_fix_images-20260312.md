# E2E Report: fortune-teller-bot (Fix: Tarot Card Images)

**Date**: 2026-03-12
**Project**: fortune-teller-bot (e0b64cd2-1673-4b40-871a-f78ce4925f9b)
**Story**: story-7bb2917a — "Fix tarot card images not displaying"
**User**: 93459832 (Telegram, Юля)
**Modules**: backend, tg_bot
**Previous stories**: story-c5f4cd46 (initial bot), story-3844bb0b (LessWrong relevance feature)

## Timeline

| Time (UTC) | Event |
|---|---|
| 23:22 | User reports: "Картинки с картами не всегда отображаются. Почини эту проблему" |
| 23:23 | PO creates story-7bb2917a, submits to architect (action=fix) |
| 23:23 | Architect creates task-0e34444e (fix images) + task-a71666c4 (CI-check) |
| 23:24 | Task 1 dispatched — **FAILS**: stale worker `dev-fortune-teller-bot-a46ef562` in Redis (container already removed) |
| 23:24 | **Manual intervention**: cleaned stale worker:status/meta from Redis |
| 23:24 | **Manual intervention**: fixed project status `developing` → `active` |
| 23:24 | **Manual intervention**: completed stuck test story-03064cc2, cancelled queued deploy |
| 23:25 | Dispatcher auto-retries task-0e34444e, new worker `dev-fortune-teller-bot-f3f76389` spawns |
| 23:25 | Image built, scaffolded workspace mounted, Claude Code agent starts |
| ~23:35 | Agent downloads 78 Rider-Waite tarot card images, modifies tarot.py + main.py, adds tests |
| ~23:37 | Agent commits `32ac96e` — "fix: switch tarot card images to local file storage with fallback" |
| 23:38 | Task 1 done, task 2 (CI-check) dispatched, reusing same worker container |
| 23:42 | CI-check done, story triggers deploy (action=feature) |
| 23:42 | Deploy workflow starts (GitHub Actions run 22979704833) |
| 23:43 | Deploy success — smoke pass (backend: HTTP 200, tg_bot: skip) |
| 23:43 | Story completed, user notified: "Deployed fortune-teller-bot: http://80.209.235.229:8002" |

## What Worked

1. **PO correctly identified the issue** — created a fix story from user's complaint about broken images
2. **Architect decomposition** — 1 fix task + 1 CI-check task, clean and minimal
3. **Agent fix structure** — added `local_image` path to every card, implemented fallback (text-only if image missing), startup validation, proper logging, 6 new tests
4. **Worker container reuse** — same container used for both tasks
5. **CI-check task** — ran tests, pushed, verified CI green
6. **Deploy auto-fallback** — correctly used `action=feature` (project status was `active` after manual fix)
7. **Smoke test** — backend health check passed
8. **User notification** — Юля received deploy success message
9. **Dispatcher auto-retry** — after first attempt failed (stale worker), supervisor retried automatically once Redis was cleaned

## Problems Found

### 1. Stale worker in Redis blocks new task dispatch
- **Severity**: critical
- **Type**: orchestrator (worker lifecycle)
- **Backlog**: new
- **Status**: ✅ FIXED (fcaf242)

Worker `dev-fortune-teller-bot-a46ef562` from the previous story was physically removed (container gone), but `worker:status:*` and `worker:meta:*` keys remained in Redis with status `RUNNING`. When engineering-worker tried to spawn a new worker, worker-manager rejected it: "Project already has active worker".

**Root cause**: Worker cleanup (container removal) doesn't reliably clean up Redis state. The system restart at 23:08 killed the container but didn't trigger the cleanup path that removes Redis keys.

**Impact**: Every new task for this project fails until manual Redis cleanup.

**Fix**: Worker-manager should implement a health-check/heartbeat pattern — if a worker:status says RUNNING but the container doesn't exist, clean up the key. Or: engineering-worker should catch "already has active worker" and attempt cleanup before retrying.

### 2. Project status stuck at `developing` after successful deploy
- **Severity**: critical
- **Type**: orchestrator (data)
- **Backlog**: existing (same as previous report #1)
- **Status**: ⬚ recurring

Project status was still `developing` instead of `active` even though fortune-teller-bot had been successfully deployed in story-3844bb0b. This caused the test story-03064cc2 deploy loop (hundreds of failed deploys with "dir already exists").

**Root cause**: Same as previous report — deploy auto-fallback `create→feature` was implemented, but the project status transition to `active` didn't happen reliably (possibly due to system restart during deploy).

### 3. Test story-03064cc2 infinite deploy retry loop
- **Severity**: major
- **Type**: orchestrator
- **Backlog**: new
- **Status**: ✅ FIXED (fcaf242)

story-03064cc2 ("Test: CI-check no-commit flow") got stuck in `deploying` with hundreds of failed deploy attempts (all "Service dir already exists"). Each dispatcher cycle (30s) created a new failed deploy run. This generated massive spam in `po:proactive` for user 93459832.

**Root cause**: Two issues compounding:
1. Project status `developing` → dispatcher uses `action=create` → fails
2. Failed deploy doesn't transition story out of `deploying` → dispatcher keeps retrying

**Fix**: Deploy failures should have a max retry count or backoff. Story should transition to `failed` after N consecutive deploy failures.

### 4. Minor Arcana images are placeholder stubs — sendPhoto 400
- **Severity**: critical
- **Type**: generated code (agent quality)
- **Backlog**: new
- **Status**: ⬚ NOT FIXED

Agent downloaded real images for **22 Major Arcana** (~1MB each, valid JPEGs from Wikimedia Commons). But for **56 Minor Arcana** it created **1964-byte placeholder stubs** — technically valid JFIF headers but too small/empty to display. Telegram rejects them with `sendPhoto 400 Bad Request`, triggering the text-only fallback.

**Evidence**: On server, all Minor Arcana files are exactly 1964 bytes. `docker logs` shows: `sendPhoto "HTTP/1.1 400 Bad Request"` → `WARNING - Failed to send local tarot card image: 07 of Pentacles` → `Sent text-only fallback`.

**Root cause**: The original Minor Arcana URLs used a broken Wikimedia `/thumb/8/8e/` path pattern (same bug described in the previous e2e report, problem #5). Agent couldn't download them and silently created placeholder stubs instead.

**Impact**: 56 out of 78 cards (72%) show text-only fallback with 🃏 emoji instead of actual card image. The user's bug report ("картинки не всегда отображаются") is only partially fixed — Major Arcana now works, Minor Arcana still broken.

**Fix**: Use correct Wikimedia Commons URLs for Minor Arcana, or use a different public domain image source. The agent's fallback mechanism works correctly — this is a data/asset problem, not a code problem.

### 5. Proactive message spam from deploy loop
- **Severity**: major
- **Type**: orchestrator
- **Backlog**: existing (spam filter was implemented but didn't catch this pattern)
- **Status**: ⬚ TODO

Despite the deploy spam filter implemented in the previous story, user 93459832 still received repeated "Deploy pre-check failed" and "All tasks done. Deploy triggered" messages — dozens of them over ~20 minutes.

**Root cause**: The spam filter blocks duplicate deploy status messages, but the deploy loop generates alternating message patterns ("tasks done" → "deploy failed" → "tasks done" → ...) which bypass deduplication.

**Fix**: Rate-limit proactive messages per user (e.g., max 1 message per 5 min for same project) or suppress all deploy-related messages except final success/failure.

### 6. Developer agent has no feedback loop / escalation mechanism
- **Severity**: critical
- **Type**: orchestrator (architecture)
- **Backlog**: new
- **Status**: ⬚ TODO

The developer agent works in a one-way flow: receives task → produces commit. When it encounters problems (e.g., 404 URLs for Minor Arcana images), it cannot escalate, ask questions, or request clarification. It silently works around the issue (creating 1964-byte placeholder stubs) instead of blocking and saying "these URLs return 404, what should I do?".

The architect also cannot verify URLs or external resources — it's a prompt-based LLM call with no internet access or tools. So neither the architect nor the developer can catch data problems like broken URLs before they ship.

**Root cause**: The pipeline is strictly sequential and unidirectional: Architect → Task → Developer → Commit. There is no back-channel for the developer to communicate blockers to the architect or PO. The developer's only output is a git commit.

**Impact**: Any task that hits an unexpected blocker (broken URLs, missing dependencies, ambiguous requirements) gets silently worked around rather than properly resolved. The user receives a "completed" notification for work that is actually incomplete or wrong. In this case, 72% of the fix was broken but the pipeline reported success.

**Fix**: Implement a developer ↔ architect dialog channel. When the developer encounters a blocker:
1. Developer publishes a "blocked" status with a description of the problem
2. Architect receives the blocker, can re-evaluate the task, provide alternative instructions, or escalate to PO
3. Developer receives updated guidance and continues

Even without internet access, an architect + developer dialog would increase the chance of finding a solution (e.g., architect could suggest alternative image sources, different URL patterns, or change the approach entirely). The key insight: two LLMs collaborating on a problem are more likely to solve it than one LLM silently guessing.

### 7. PO always creates new story — no reopen, no dedup, no context carryover
- **Severity**: major
- **Type**: orchestrator (PO agent)
- **Backlog**: new
- **Status**: ⬚ TODO

If user reports the same bug again (e.g., "images still broken"), PO will create a brand new story instead of reopening the existing completed one. The architect will then create tasks from scratch with no context of what was already tried.

**Evidence**: PO's `create_story()` tool always calls `POST /api/stories/` without checking `list_stories()` first. The system prompt has no instruction to look for existing stories. Stories API has no `/reopen` endpoint (though `COMPLETED → IN_PROGRESS` is a valid transition in `VALID_TRANSITIONS`).

**Impact**: Context of previous attempts is lost. The new developer gets the same task description, may try the same broken approach, and produce the same result. For iterative fixes (like this image bug), each attempt starts from zero.

**Fix**:
1. Add instruction in PO prompt to call `list_stories()` before `create_story()` and check for recent completed/failed stories with similar scope
2. Add `/reopen` endpoint for stories (transition `completed → in_progress`)
3. When reopening, architect should see previous tasks + their events/results to inform new task descriptions

### 8. Worker reports no intermediate progress — problems are invisible
- **Severity**: major
- **Type**: orchestrator (observability)
- **Backlog**: new
- **Status**: ⬚ TODO

Task events for `task-0e34444e` contain only status transitions (`todo → in_dev → in_ci → done`) and final `iteration_end` with commit SHA. No record of the 56 failed image downloads, no mention of placeholder stubs, no indication that 72% of the work was incomplete.

**Evidence**: `GET /api/tasks/task-0e34444e/events` returns 9 events — all `status_change` or `iteration_end`. `failure_metadata: null`. The infrastructure exists (`TaskEvent` model supports `note`/`comment` types with JSON `details`), but nothing populates it during agent execution.

**Root cause**: Three gaps in the reporting chain:
1. `orchestrator-cli` (tools available to Claude Code inside worker) has no "report problem" or "log progress" command
2. `worker-wrapper` only publishes lifecycle events (started/completed/failed), not intermediate progress
3. `engineering consumer` only records events for CI failures and worker rejections, not for agent-reported issues

**Impact**: From the orchestrator's perspective, this task was a complete success. The only way to discover the 72% failure rate was to SSH into the prod server and check file sizes manually. No automated system detected or flagged the problem.

**Fix**: Add a `report-progress` or `log-issue` tool to `orchestrator-cli` that writes `TaskEvent(event_type="note")` via the API. The developer agent's system prompt should instruct it to report significant problems (failed downloads, missing resources, workarounds applied). This also feeds into the feedback loop (problem #6) — reported issues could trigger architect review.

## Summary

- **Fix task**: 1/1 completed by automation in ~12 min — code fix correct, but 56/78 images are placeholder stubs (Minor Arcana)
- **CI-check task**: Completed in ~4 min
- **Deploy**: Succeeded (action=feature), smoke pass
- **Total time**: ~21 min (23:22 → 23:43) — 20 min automated, 1 min manual intervention
- **Manual interventions**: 3 (clean stale Redis worker, fix project status, clean stuck test story)
- **User notified**: Yes

## Recommendations

### From previous reports — still open:
13. ⬚ **LOW**: Story completion race — deploy worker and dispatcher both try to complete story

### New from this run:
14. ✅ **CRITICAL**: Stale worker Redis cleanup — `_check_project_lock()` auto-cleans DEAD/FAILED/STOPPED workers (fcaf242)
15. ✅ **HIGH**: Deploy retry limit — max 3 retries per story, then story → failed (fcaf242)
16. ⬚ **MEDIUM**: Proactive message rate-limiting per user/project — suppress repetitive deploy status messages
17. ⬚ **CRITICAL**: Minor Arcana images are stubs — need real images. Agent created 1964-byte placeholders for 56/78 cards because Wikimedia thumb URLs are broken. User's bug is only 28% fixed (Major Arcana only)
18. ⬚ **CRITICAL**: Developer feedback loop — developer agent has no escalation mechanism. When tasks hit blockers (404 URLs, missing deps, ambiguous requirements), developer silently works around instead of blocking. Need developer ↔ architect dialog channel so blockers get resolved instead of hidden
19. ⬚ **HIGH**: PO story dedup/reopen — PO always creates new stories, never checks for existing ones. Add `list_stories()` check before `create_story()`, add `/reopen` endpoint, carry over context from previous attempts
20. ⬚ **HIGH**: Worker progress reporting — task events contain only status transitions, no intermediate progress. Add `report-progress` tool to orchestrator-cli so agent can log problems (failed downloads, workarounds) as task events
