# E2E Report: fortune-teller-bot

**Date**: 2026-03-11
**Project**: fortune-teller-bot (e0b64cd2-1673-4b40-871a-f78ce4925f9b)
**Story**: story-c5f4cd46 — "Create fortune telling Telegram bot"
**User**: 93459832 (Telegram)
**Modules**: backend, tg_bot

## Timeline

| Time (UTC) | Event |
|---|---|
| ~17:15 | PO creates project via `create_project` |
| 17:15-17:28 | PO stores secrets: OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN |
| 17:29 | Architect creates 3 tasks, dispatcher starts task 1 |
| 17:35 | Task 1 done (prediction history model), commit eab6731, CI passed |
| 17:36 | Task 2 dispatched (main bot logic) |
| 17:49 | Task 2 done (full bot implementation), commit a07cb7c, CI passed |
| 17:50 | Task 3 dispatched (CI check) |
| 17:51-17:58 | Task 3 fails 4 times — "Worker reported success but no commit was made" |
| 17:58 | Story fails — retries exhausted on task 3 |
| 18:01 | Manual deploy triggered (action=create) |
| 18:04 | Deploy failed — tg_bot container crashes on startup |
| 18:06 | Re-deploy (action=feature) + rerun — same crash |
| 18:08 | Deploy failed again — tg_bot crash is a code bug |
| 18:28 | Manual fix: remove nonexistent shared.generated.events import |
| 18:29 | CI failed — tests reference deleted post_init/post_shutdown |
| 18:29 | Manual fix: remove broker-related tests |
| 18:31 | CI green, deploy fails — tg_bot still crashes (relative imports!) |
| 18:41 | Manual fix: change relative imports to absolute (src.module) |
| 18:44 | CI green, deploy fails — src.module not resolvable from PYTHONPATH |
| 18:49 | Manual fix: use fully qualified imports (services.tg_bot.src.module) |
| 18:51 | CI green |
| 18:52 | Deploy started with action=feature |
| 18:53 | **Deploy SUCCESS** — all containers running, smoke pass |
| 18:53 | Story story-9812cbad completed, project status → active |

## Problems Found

### 1. tg_bot crashes on startup — missing `shared.generated.events`
- **Severity**: critical
- **Type**: template
- **Backlog**: new
- **Status**: ✅ FIXED (2 fixes applied)
  1. **Deploy→engineering feedback loop** — deploy worker now re-dispatches fix task to `engineering:queue` on smoke/workflow failure, max 2 attempts. TDD: 7 unit tests passing.
  2. **Root cause (service-template)** — removed `**/generated/` from template `.gitignore`. E2E verified: RED (tg_bot crash `ModuleNotFoundError`) → GREEN (containers start, imports resolve). Full pipeline: copier → framework.generate → docker build → push to registry → deploy to server.

The generated code imports `from shared.generated.events import get_broker` in `services/tg_bot/src/main.py:23`, but `shared/generated/` directory does not exist in the repo. Only `shared/shared/__init__.py` and `shared/shared/http_client.py` exist.

The import fails at startup, crashing the container before the bot can initialize. This blocks the entire tg_bot service from running.

**Root cause**: The Claude Code agent added event bus integration (Redis broker connect/disconnect in `post_init`/`post_shutdown`) using a module path that doesn't exist. The scaffold creates `shared/spec/events.yaml` but `make generate-from-spec` either wasn't run or doesn't generate `shared/generated/events.py`.

**Fix**: Either (a) remove the `get_broker()` import and `post_init`/`post_shutdown` hooks if events aren't needed, or (b) ensure `make generate-from-spec` generates the events module.

### 2. CI-check task loops forever — "no commit made" on verification-only tasks
- **Severity**: major
- **Type**: orchestrator
- **Backlog**: new
- **Status**: ✅ FIXED (`allow_no_commit` flag in EngineeringState + developer node + engineering consumer)

Task 3 ("Run tests, verify CI green") is a verification-only task that doesn't produce code changes. The engineering worker requires a commit as proof of work (`developer_node_no_commit` error). When the agent reports "all tests pass, CI is green" but makes no commit, the worker marks it as failed.

The task retried 3 times (the maximum) and exhausted retries, causing the entire story to fail — even though all actual code tasks were complete and CI was green.

**Root cause**: Engineering worker has a hard requirement: `Worker reported success but no commit was made`. Verification/CI-check tasks that find nothing to fix will always hit this.

**Fix options**:
1. Architect should not create separate "Run tests, verify CI green" tasks — CI verification is already part of each task's iteration cycle (in_dev → in_ci → testing → done)
2. Engineering worker should handle "success with no commit" as a valid outcome for verification-type tasks
3. Worker could create an empty commit (e.g., adding a VERIFIED file) but this is hacky

### 3. PO uses project name as ID before project creation
- **Severity**: minor
- **Type**: orchestrator
- **Backlog**: —
- **Status**: ⬚ known, PO self-corrects — low priority

PO agent called `set_project_secret("fortune-teller-bot", ...)` three times using the project name instead of UUID, getting 422 errors each time. Then created the project and used the correct UUID.

This is a known PO prompt issue — the agent tries to store secrets before having a project ID. Doesn't block user experience since PO self-corrects.

### 4. Deploy action detection — create vs feature race condition
- **Severity**: minor
- **Type**: orchestrator
- **Backlog**: —
- **Status**: ⬚ TODO

After the first deploy (action=create) failed due to tg_bot crash, the deploy worker's automatic retry used action=create again, hitting precheck failure "Service dir already exists". The scheduler also triggered a new deploy with action=create because `project.status` was still `developing` (not updated to `active` since deploy never succeeded).

Manual intervention with `action=feature` was needed. This is expected behavior for first deploys that partially succeed (files deployed but containers crash), but could be smoother.

### 5. User receives technical spam during deploy failures
- **Severity**: major
- **Type**: orchestrator
- **Backlog**: new
- **Status**: ⬚ TODO

During the deploy retry loop, the user received **7 technical messages** in Telegram before the final success:
- 4× "All 1 tasks done. Deploy triggered (create)."
- 3× "Deploy pre-check failed: Service dir /opt/services/fortune-teller-bot/ already exists on 80.209.235.229. Clean up the previous deployment or use action='feature'."

These are internal system messages that should never reach the user. The user has no idea what "action=create" or "Service dir already exists" means.

The final success message was also too dry: `Deployed fortune-teller-bot: http://80.209.235.229:8002` — no bot link, no explanation of what to do next.

**Root cause**: `po:proactive` stream forwards raw deploy status messages to PO, and PO relays them verbatim. There's no filtering of technical/internal messages, and no enrichment of success messages.

**Fix**: Stop publishing deploy status to `po:proactive` entirely. Only two proactive events should reach the user:
1. **Story completed** — include bot's Telegram link (`https://t.me/bot_username`), not raw server URL.
2. **Story permanently failed** (retries exhausted, no auto-recovery) — simple apology, no technical details.

PO already has reminders + `get_story` — it can poll for intermediate status on its own schedule. All deploy retries, precheck errors, and intermediate failures are internal and must never surface to the user.

### 6. PO does not validate bot token or extract bot username
- **Severity**: major
- **Type**: orchestrator
- **Backlog**: new
- **Status**: ⬚ TODO

PO stores `TELEGRAM_BOT_TOKEN` as a secret but never validates it. If the token is invalid or expired, this is only discovered at deploy time when the tg_bot container crashes — after all code tasks and CI have completed.

Additionally, the bot's Telegram username (`@bot_name`) is never extracted or stored, so the system cannot tell the user where their bot lives.

**Proposed fix**: After `set_project_secret(TELEGRAM_BOT_TOKEN)`, PO should immediately call `https://api.telegram.org/bot<token>/getMe` via its `web_search` or a new `validate_telegram_token` tool:
1. **If success** — extract `result.username`, store as `TELEGRAM_BOT_USERNAME` env var, and confirm to user: "Token valid, your bot is @bot_name".
2. **If failure** — tell the user the token is invalid and ask for a new one. Do NOT proceed to story creation.

**Benefits**:
- Early token validation (fail fast at PO stage, not at deploy)
- Bot username available for deploy success messages ("Your bot is live: https://t.me/bot_name")
- Bot username available for future Telethon-based smoke tests after deploy

## Summary

- **Code tasks**: 2/2 completed successfully by automation, CI green
- **CI-check task**: Failed due to "no commit" requirement (orchestrator bug) — manually resolved
- **Deploy (auto)**: Failed 5 times due to code bugs in generated tg_bot service
- **Deploy (manual fix)**: Succeeded after 4 manual commits fixing import issues
- **Final result**: Project deployed and operational at http://80.209.235.229:8002
- **Total time**: ~1h 40min (17:15 → 18:53) — most spent on deploy debugging
- **Data fixes applied**: Manual task transitions (task 3: failed→done), story recovery (DB: failed→in_progress), project status (DB: developing→active)

### Root Causes of tg_bot Crash (3 layers)
1. `from shared.generated.events import get_broker` — module doesn't exist
2. Relative imports (`from .lesswrong`) — fail when run as script via `python main.py`
3. Wrong absolute imports (`from src.module`) — not in PYTHONPATH

### Working Import Pattern
`from services.tg_bot.src.module import ...` — works both in Docker (PYTHONPATH=/app) and CI (PYTHONPATH=.)

## Recommendations

1. ✅ **CRITICAL**: Deploy→engineering feedback loop — deploy worker re-dispatches fix task to `engineering:queue` on smoke/workflow failure (max 2 attempts). Implemented + 7 unit tests.
2. ✅ **CRITICAL**: `.gitignore` fix — removed `**/generated/` from service-template. E2E RED→GREEN verified on real server.
3. ✅ **HIGH**: CI-check task no longer fails on "no commit" — `allow_no_commit` flag allows verification-only success
4. ⬚ **HIGH**: PO must validate bot token via Telegram `getMe` API immediately after receiving it. Store `TELEGRAM_BOT_USERNAME` as env var. Fail fast if token is invalid — don't waste CI/deploy cycles on a bad token
5. ⬚ **HIGH**: Agent must not import `shared.generated.events` if it wasn't generated — need guard or AGENTS.md doc
6. ⬚ **MEDIUM**: Document import pattern in template AGENTS.md — no relative imports in services run via `python file.py`
7. ⬚ **HIGH**: Stop pushing deploy status to `po:proactive`. PO should poll story status via reminders if it wants updates. Only two events should reach the user proactively: (a) story completed — "your bot is live: t.me/...", (b) story permanently failed (retries exhausted, no auto-recovery possible) — "sorry, couldn't complete, admin will look into it". Everything else (deploy retries, precheck errors, intermediate failures) is internal
8. ⬚ **MEDIUM**: Add container crash logs to deploy failure output (currently just "FAILED: ['infra-tg_bot-1']" with no details)
9. ⬚ **MEDIUM**: Deploy action detection bug — after first failed deploy creates dir on server, subsequent deploys still use action=create because project.status isn't updated until deploy succeeds
10. ⬚ **LOW**: Allow `failed` → `in_progress` story transition for manual recovery
