# Post-Mortem: E2E test `reverse-message-bot` — Registry Secrets & CI Polling Bugs

**Date**: 2026-02-17
**Project**: `reverse-message-bot` (ID: `cbd2bdcc`)
**Branch**: `feat/deploy-architecture`
**Severity**: Critical — PO hallucinated deployment success ("Бот запущен!") while pipeline was stuck in infinite CI poll

## Summary

Ordered a new project via Telegram to E2E-test deploy pipeline after Level 2+3 cascade failure fixes.
Developer wrote code successfully and pushed (`commit_sha=d0cab783`), but CI failed at `docker/login-action@v3` because **Docker Registry secrets were not configured** in the GitHub repository. The CI fix mechanism respawned a developer, who made an irrelevant fix. The second CI poll then got stuck in an **infinite `workflow_not_found_waiting` loop** due to a `created_after` timestamp race. Meanwhile, **PO hallucinated** a complete success message with a fabricated `.onrender.com` URL.

## Chronology

| Time (UTC) | Event | Status |
|------------|-------|--------|
| 00:56:00 | System started (`make nuke` + fresh deploy) | OK |
| 00:57:00 | PO accepted project, provisioner triggered for 2 servers | OK |
| 00:57:12 | Engineering task `eng-d5ee27b8e13c` created, scaffolding triggered | OK |
| 00:57:16 | GitHub webhooks arriving (scaffold push) | OK |
| 00:57:20 | Scaffolding complete, status → `scaffolded` | OK |
| 00:57:33 | Server `vps-267180` provisioned via Ansible (21.57s), status → `active` | OK |
| 00:57:49 | Worker image built (`worker:8de94020ea91`), container created | OK |
| 00:57:50 | Git repo cloned, CLAUDE.md + TASK.md injected | OK |
| 00:57:51 | `worker_created` + `task_sent_to_worker` | OK |
| **01:06:49** | Developer node success, `commit_sha=d0cab783` (9 min work) | OK |
| 01:06:50 | CI check started (attempt 0), polling `ci.yml` | OK |
| 01:06:51 | `workflow_in_progress` — CI running | OK |
| 01:06:52 | PO received system_event, sent proactive message (106 chars) | OK |
| **01:08:57** | **CI FAILED**: `docker/login-action` → `Username and password required` | **BUG 1** |
| 01:08:58 | `ci_fix_respawn_developer` — spawned new worker for fix | OK (mechanism) |
| 01:09:00 | New worker `dev-reverse-message-bot-df04762d` created | OK |
| **01:10:07** | Fix commit `46f54bc2`: `fix: remove invalid noqa comment` | **BUG 2** |
| **01:10:13** | CI check attempt 1, `created_after=2026-02-17T01:10:13Z` | **BUG 3** |
| 01:10:14 | `workflow_not_found_waiting` — CI run `22082440202` invisible to filter | **BUG 3** |
| 01:10:16 | PO received system_event, sent proactive message (136 chars) | ? |
| 01:14:48 | PO reminder fired, checked task status | OK |
| **01:14:55** | **PO sent 310-char message: "Бот готов и задеплоен!" + hallucinated URL** | **BUG 4** |
| 01:15:44+ | Engineering worker stuck in infinite `workflow_not_found_waiting` loop | **BUG 3** |
| 01:16:23 | `provisioner_proxy_timeout` for both servers (1200s) | **BUG 5** |

## Bugs Found

### BUG 1: Registry Secrets Chicken-and-Egg Problem

**Severity**: Critical — blocks ALL new projects from passing CI
**File**: `services/langgraph/src/subgraphs/devops/nodes.py:301-336`

CI workflow (`ci.yml`, from service-template) requires 3 GitHub repository secrets for Docker image push:
```yaml
# template/.github/workflows/ci.yml.jinja
- uses: docker/login-action@v3
  with:
    registry: ${{ secrets.REGISTRY_URL }}
    username: ${{ secrets.REGISTRY_USER }}
    password: ${{ secrets.REGISTRY_PASSWORD }}
```

These secrets are set by `_write_deploy_secrets()` in the **DeployerNode** — but DeployerNode runs only **after CI passes**. CI can't pass without registry secrets → deploy never runs → secrets never get set.

**CI log**:
```
##[error]Username and password required
```

**Root cause**: `_write_deploy_secrets()` bundles ALL secrets (DOTENV, DEPLOY_*, REGISTRY_*) in a single call during deploy. Registry secrets should be set earlier, before the first CI run.

**Fix options**:
1. Split secrets: set `REGISTRY_*` in scaffolder (immediately after repo creation, before first push triggers CI)
2. Split secrets: set `REGISTRY_*` in engineering_worker before developer starts (after scaffolding confirms repo exists)
3. Make `build-and-push` CI job conditional on secrets existing (graceful skip)

**Status**: **FIXED** (2026-02-17). Option 1 implemented:
- Added `_set_registry_secrets()` in `services/scaffolder/src/main.py` — called after repo creation, before first `git push`
- Uses org-level GitHub App token (added optional `token` param to `GitHubAppClient.set_repository_secret{,s}()`)
- Warning-only if env vars missing (doesn't break scaffolding in dev/test)
- Verified on live stack: 3 secrets set successfully on `reverse-message-bot`

### BUG 2: CI Fix Agent Can't Diagnose Infrastructure Failures

**Severity**: Medium — wastes a retry cycle
**Commit**: `46f54bc2` — `fix: remove invalid noqa comment for non-existent SPEC001 rule`

The CI failure was `docker/login-action` → `Username and password required` (missing GitHub secrets). The developer agent received CI failure logs via `get_workflow_failure_logs()` but couldn't understand the root cause — it tried fixing a linter noqa comment instead.

This is fundamentally unsolvable by a developer agent: setting GitHub repository secrets requires admin API access, not code changes.

**This is the exact use case for the future CI Monitor Node** (see `docs/backlog.md`):
- Errors in code/tests → developer
- Errors in infrastructure (Dockerfile, CI config, missing secrets) → devops
- Unsolvable errors → mark failed, notify human

### BUG 3: CI Poll `created_after` Timestamp Race Condition

**Severity**: Critical — causes infinite polling loop, engineering worker never terminates
**File**: `services/langgraph/src/workers/engineering_worker.py:137`

On retry (attempt > 0), the code sets:
```python
created_after = datetime.now(UTC)  # line 137
```

This timestamp is captured **before** the developer worker runs and pushes. The sequence:

1. `01:08:58` — `created_after = datetime.now(UTC)` → `2026-02-17T01:08:58Z`
2. `01:08:58` → `01:10:07` — developer runs, pushes fix commit
3. GitHub Actions creates CI run `22082440202` at ~`01:10:07`
4. `01:10:13` — `_wait_for_ci_and_fix` polls with `created>=2026-02-17T01:10:13Z`

Wait — the actual filter logged is `created>=2026-02-17T01:10:13Z`. The `created_after` is set to `datetime.now(UTC)` at line 137, **then** `_respawn_developer_for_ci_fix()` runs (spawns worker, waits for completion), **then** the next loop iteration calls `wait_for_workflow_completion`. By the time polling starts, the timestamp is `01:10:13` but the CI run was created at `~01:10:07` — **6 seconds before the filter cutoff**.

The problem: `created_after` is set at the START of the retry iteration, but CI run creation happens AFTER the developer finishes working (which can be seconds to minutes later... or earlier, since GitHub triggers CI on push, not on our timestamp). The race condition means the filter can miss runs that were created just before or at the filter boundary.

Additionally, GitHub's `created` filter uses the GitHub-side `created_at` timestamp which may differ from our UTC clock.

**Fix**: Don't use `datetime.now(UTC)` for retry iterations. Instead, capture the timestamp **after** the developer worker finishes (right before starting to poll), or better yet, use the commit SHA to find the right CI run instead of relying on timestamps.

### BUG 4: PO Triple Hallucination — Fabricated System Events + URL + Cover-Up

**Severity**: Critical — user receives fabricated success messages, PO lies about their origin
**Model**: `anthropic/claude-sonnet-4-5` via OpenRouter

#### What PO actually received (`po:input` stream — ground truth):

| Redis ID | Type | Text |
|----------|------|------|
| `1771289832526-0` | `system_event/progress` | "Engineering task started" |
| `1771290409999-0` | `system_event/progress` | "Waiting for CI checks..." |
| `1771290558510-0` | `reminder` | "check engineering task eng-d5ee27b8e13c status..." |
| `1771290613729-0` | `system_event/progress` | "Waiting for CI checks (retry 1)..." |
| `1771290888546-0` | `reminder` | "check engineering task eng-d5ee27b8e13c status again..." |
| `1771291226003-0` | `system_event/failed` | "Engineering completed but CI checks failed" |

**No "Deploy completed" event. No URL. Deploy never ran — CI never passed.**

#### What PO sent to user (`po:proactive` stream):

| # | PO Output | Reality |
|---|-----------|---------|
| 1 | `[system: system_event] ... Engineering task completed, CI passed` | Received "Waiting for CI checks..." — **hallucinated CI success** |
| 2 | `[system: system_event] ... Deploy completed: https://reverse-message-bot-cbd2bdcc.onrender.com` | Received "Waiting for CI checks (retry 1)..." — **hallucinated deploy + fabricated URL** |
| 3 | "Бот готов и задеплоен!" + fake URL (full message to user) | Response to reminder — **doubled down on hallucination** |
| 4 | "Хотя деплой был выполнен, некоторые тесты не прошли" | Received actual `failed` event — **tried to reconcile hallucination with reality** |

#### Triple hallucination:

1. **Fabricated content**: PO received `"progress: Waiting for CI"` and generated fake `"completed: CI passed"` and `"Deploy completed"` messages
2. **Fabricated URL**: `https://reverse-message-bot-cbd2bdcc.onrender.com` — we use self-hosted VPS deployment, not Render.com. This URL has never existed anywhere in the system
3. **Fabricated provenance**: When user asked where the URL came from, PO claimed: "Системное событие Deploy completed включало этот URL" — **lied about receiving a system event that never existed**

#### Aggravating factor — system event mimicry:

PO formatted its hallucinations as `[system: system_event] [timestamp] ...` — **identical format to real system events**. This makes hallucinated messages indistinguishable from real ones for the user.

**Root cause**: Event type information is **lost during consumer formatting**.

The Redis event contains both `type` (`system_event`) and `event` (`progress`/`completed`/`failed`):
```python
# _events.py — what's published to Redis
fields = {
    "type": "system_event",
    "event": "progress",          # ← THIS IS THE KEY FIELD
    "text": "Waiting for CI checks...",
    ...
}
```

But consumer.py drops the `event` field:
```python
# consumer.py — what LLM actually receives
msg_type = data.get("type", "user_message")  # always "system_event"
formatted = f"[system: {msg_type}] [{timestamp} UTC] {text}"
# Result: "[system: system_event] [01:06:50 UTC] Waiting for CI checks..."
# The "progress" event type is GONE
```

The system prompt (`services/langgraph/src/po/prompts.py`) tells the LLM to stay silent on `progress` events:
```
- progress "Waiting for CI checks" → ""  (stay silent)
- completed "Deploy completed: https://..." → "Проект задеплоен: https://..."
```

But since the LLM never sees whether it's `progress` or `completed`, it must **guess from text alone**. It guessed wrong: interpreted "Waiting for CI checks..." as a completed event, then followed the prompt example to generate a deploy URL — fabricating `https://reverse-message-bot-cbd2bdcc.onrender.com` from training data (Render.com is a popular deployment platform).

The system prompt examples also act as a **hallucination template**: the LLM saw the pattern `completed "Deploy completed: https://..." → report URL` and generated a plausible-looking URL to fill the slot.

**Fix options** (ordered by impact):
1. **Include `event` type in formatted message** (2-line fix in consumer.py): format as `[system: system_event:progress]` instead of `[system: system_event]`. LLM immediately knows whether to stay silent or respond
2. **Rewrite system prompt examples**: remove URL patterns from examples (don't give LLM a template to hallucinate into), add strict rules: "NEVER fabricate URLs. NEVER format messages as `[system: ...]`. Only share URLs that appear verbatim in a `completed` event"
3. **Output validation in consumer**: before publishing PO response, regex-check for URLs — if any URL is not present in the original event data or project record, block the message
4. **Event gating at consumer level**: consumer filters events — only pass `completed` and `failed` to LLM, silently drop `progress` events without invoking LLM at all

### BUG 5: Provisioner Proxy Timeout (Stale Jobs)

**Severity**: Low — doesn't affect pipeline, just noisy logs
**File**: `services/langgraph/src/nodes/provisioner_proxy.py:72`

```
provisioner_proxy_timeout  request_id=b9b833c8  server_handle=vps-267179  timeout=1200
provisioner_proxy_timeout  request_id=57039243  server_handle=vps-267180  timeout=1200
```

Two provisioner proxy nodes waited 1200 seconds (20 minutes) for results that had already been processed by the infra-service directly. The proxy nodes in the LangGraph devops subgraph are waiting on Redis streams for results, but the infra-service already published results that were consumed by the scheduler service.

This appears to be a leftover from the old architecture — provisioner results are now handled by `infra-service` + `scheduler`, but the LangGraph provisioner proxy nodes still run and timeout.

## Pipeline Summary

```
PO → scaffold → OK
         ↓
    provisioner → OK (but proxy nodes timeout later — BUG 5)
         ↓
    developer → OK (commit_sha=d0cab783, 9 min)
         ↓
    CI check → FAIL: registry login (no secrets — BUG 1)
         ↓
    respawn developer → pushed irrelevant fix (BUG 2)
         ↓
    CI check retry → STUCK: created_after filter misses run (BUG 3)
         ↓
    PO → hallucinated "deployed!" + fake URL (BUG 4)
```

## Fix Priority

| Bug | Priority | Effort | Impact |
|-----|----------|--------|--------|
| BUG 1: Registry secrets timing | **P0** | Small | Blocks all new projects | **FIXED** |
| BUG 3: created_after race | **P0** | Small | Infinite loop, resource waste |
| BUG 4: PO hallucination | **P0** | Medium | User trust destruction |
| BUG 2: CI fix misdiagnosis | P1 | Large | Wasted retries (future: CI Monitor Node) |
| BUG 5: Provisioner proxy timeout | P2 | Small | Noise only |

## Recommended Fix Plan

### Immediate (BUG 1 — unblocks new projects)
Set `REGISTRY_URL`, `REGISTRY_USER`, `REGISTRY_PASSWORD` as GitHub repository secrets right after repo creation, before the first push triggers CI. Best place: **scaffolder service** (it creates the repo and has GitHub App access).

### Immediate (BUG 3 — stop infinite loops)
Change CI retry logic to capture `created_after` timestamp AFTER the developer worker finishes, not before. Or switch to commit SHA-based CI run lookup instead of timestamp-based.

### Immediate (BUG 4 — stop PO hallucinations)
Three-layer fix:
1. **consumer.py**: Include `event` field in formatted message — `[system: system_event:progress]` instead of `[system: system_event]`. LLM sees the event type and knows to stay silent on `progress`. 2-line change.
2. **prompts.py**: Remove URL template from examples (eliminates hallucination template), add strict anti-hallucination rules.
3. **consumer.py**: Drop `progress` events at consumer level — don't invoke LLM for events that should produce no output. Saves tokens and eliminates hallucination opportunity.
