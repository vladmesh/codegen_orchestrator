# E2E Report: reverse-bot — Full pipeline with CI gate

> **Date**: 2026-03-10
> **Project**: reverse-bot (project_id: `f912befd-cab4-4d4c-800a-fe9d8199aa5a`)
> **Story**: story-0570bf42 — "Create reverse-bot"
> **Mode**: Full Pipeline (PO → Scaffolder → Architect → Dispatcher → Worker → CI Gate)
> **Status**: ❌ FAILED — infinite retry loop on infrastructure CI failure

---

## Timeline

| Time (UTC) | Event |
|------------|-------|
| 16:13:00 | PO: notify_user, create project + repository (`repo-f343839c`) |
| 16:13:06 | PO: story `story-0570bf42` created, submitted to architect |
| 16:13:08 | PO: reminder set (12 min) |
| 16:13:35 | Scaffolder: `scaffold_complete` (90 lines tree), project status → `scaffolded` |
| 16:13:50 | Architect: created tasks, dispatcher dispatched `task-cad97608` → `eng-c070469adcdf` |
| 16:13:50 | Engineering worker: resources allocated (backend:8000, tg_bot:8001 on vps-267180) |
| 16:13:52 | Worker container `dev-reverse-bot-7d30d0f2` created |
| 16:13:53 | Worker: workspace preflight passed, Claude Code started |
| 16:21:07 | Worker: feature implemented, commit `bb56f0f` pushed |
| 16:21:08 | CI gate: waiting for ci.yml |
| 16:21:24 | CI gate: **ci_check_failed** (attempt 0) — lint failed |
| 16:21:25 | CI gate: sent CI fix task to worker (attempt 1, `--resume` session) |
| 16:23:16 | Worker: fix commit `80e3567` (deptry false-positive) → CI failure |
| 16:25:40 | Worker: fix commit `e3627ff` (skip bot startup w/o token) → CI failure |
| 16:27:48 | Worker: fix commit `8e0b3cc` (xenon complexity) → CI failure |
| 16:30:55 | Worker: fix commit `1f41cd6` (TELEGRAM_BOT_TOKEN in .env.example) → CI failure |
| 16:33:16 | CI gate: **ci_check_failed** (attempt 1) — build-and-push failed (registry auth) |
| 16:33:17 | CI gate: `ci_infra_rerun_attempting` — reran failed jobs |
| 16:34:08 | CI gate: `ci_infra_rerun_failed` — same registry auth failure |
| 16:34:08 | CI gate: **ci_gate_failed** → task-cad97608 → `failed` |
| 16:34:58 | Dispatcher: re-dispatched same task (supervisor moved it back to backlog→todo) |
| 16:35:55 | Worker: **no_commit** ("Everything is already implemented") → `failed` |
| 16:36:29 | Dispatcher: re-dispatched AGAIN → same result → **infinite loop** |

---

## What Worked

1. **PO → Scaffolder**: Smooth. Project created, repo scaffolded (90-line tree), secrets saved.
2. **Architect**: Created 2 tasks with correct ordering.
3. **Worker spawn & reuse**: Container created once, reused across all tasks via `story_worker_registry`.
4. **Feature implementation**: Claude Code implemented the handler correctly (~7 min).
5. **CI gate detection**: Correctly identified lint failures vs. infra failures (build-and-push).
6. **CI fix loop inside worker**: Claude Code (via `--resume`) self-healed lint issues across 4 commits.
7. **Infra rerun**: CI gate correctly detected Docker login as infra issue and triggered `rerun-failed-jobs`.

---

## Issues Found

### Issue 1: Pre-push hooks silently skip all checks in worker container (CRITICAL)

**Symptom**: Agent pushed code with lint errors that should have been caught locally.

**Root cause**: The pre-push hook in `.githooks/pre-push` (from service-template) has a native fallback:

```bash
if ! $DOCKER_AVAILABLE; then
    if command -v ruff >/dev/null 2>&1; then
        ruff check .
    else
        echo "WARNING: Neither Docker nor ruff available, skipping checks"
    fi
    exit 0  # ← always exits 0, even if nothing was checked!
fi
```

In the worker container:
- **Docker**: not available (no Docker-in-Docker)
- **ruff**: exists at `.venv/bin/ruff` but NOT in `$PATH`
- **uv**: exists, `uv tool run ruff` works
- **Result**: hook prints a warning and exits 0 → all checks bypassed

**Impact**: 3 extra CI fix commits that burned tokens and time (~10 min wasted).

**Fix options**:
1. Hook should activate `.venv/bin` or use `make lint` in native fallback
2. Hook should `exit 1` if no lint tools available (fail-safe)
3. Install ruff globally in worker image (`uv tool install ruff`)

**Where**: `/home/vlad/projects/service-template/template/.githooks/pre-push`

---

### Issue 2: Integration tests crash on invalid TELEGRAM_BOT_TOKEN (HIGH)

**Symptom**: `aiogram.utils.token.TokenValidationError: Token is invalid!` during integration tests in CI.

**Root cause**: The generated project's lifespan initializes the Telegram bot at startup. In CI, `TELEGRAM_BOT_TOKEN` is either unset or set to a placeholder (`change-me`). The aiogram library validates token format (`123456789:AABBCC...`) and crashes.

**Chain of responsibility**:
- **service-template**: `.env.test.jinja` doesn't include `TELEGRAM_BOT_TOKEN` override
- **Scaffolder**: generates `.env` with placeholder values but `.env.test` is incomplete
- **CI workflow**: `compose.tests.integration.yml` loads `env_file: [../.env, ./.env.test]` — token falls through to invalid value from `.env`

**Fix**: Add `TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz` (valid format, fake) to `.env.test.jinja` in service-template. Or: make bot startup gracefully skip when token is invalid format (not just missing).

**Where**: `/home/vlad/projects/service-template/template/infra/.env.test.jinja`

---

### Issue 3: Docker Registry secrets missing on repo (HIGH)

**Symptom**: `Must provide --username with --password-stdin` in CI build-and-push step.

**Root cause**: `REGISTRY_USER` GitHub secret is empty/missing on the `reverse-bot` repo. The scaffolder's `consumer.py:83-106` sets registry secrets during scaffold, reading from env vars `REGISTRY_USER`, `REGISTRY_PASSWORD`, `ORCHESTRATOR_HOSTNAME`. If any of these are empty in the orchestrator's `.env`, the secret is set as empty string → Docker login fails.

**Investigation needed**: Check whether orchestrator's `.env` has valid `REGISTRY_USER` / `REGISTRY_PASSWORD` / `ORCHESTRATOR_HOSTNAME`. Also check scaffolder logs for secret-setting confirmation.

**Where**: `services/scaffolder/src/consumer.py:83-106`, orchestrator `.env`

---

### Issue 4: Infinite retry loop on infrastructure failures (CRITICAL)

**Symptom**: After `ci_gate_failed`, task transitions `failed → backlog → todo → in_dev → failed → ...` endlessly.

**Root cause**: When CI gate fails, task is set to `failed`. A supervisor (likely in scheduler) automatically moves `failed` tasks back to `backlog → todo`. Dispatcher picks it up again. Worker finds nothing to do (code already pushed) → `developer_node_no_commit` → `failed` again. Cycle repeats.

**Specific flow observed**:
```
ci_gate_failed → task → failed
supervisor → task → backlog → todo
dispatcher → engineering:queue
worker: "Everything is already implemented" → no commit → failed
supervisor → task → backlog → todo
dispatcher → engineering:queue  (repeat forever)
```

**Impact**: Burns API tokens, wastes compute, worker container stays alive indefinitely.

**Fix needed**:
1. `ci_gate_failed` with `infra` reason should NOT be auto-retried by supervisor
2. Need a `failed_infra` terminal state, or a retry counter that limits re-dispatches
3. Worker should detect "already implemented, CI issue is infra" and REJECT rather than report no_commit

**Where**: `services/langgraph/src/consumers/engineering.py`, `services/scheduler/src/tasks/task_dispatcher.py`

---

### Issue 5: CI fix prompt only shows original failure URL (LOW)

**Symptom**: CI fix prompt (attempt 1) references original run URL `22912633155` even though there were 4 subsequent runs. Claude Code has to discover later runs itself via `gh run list`.

**Context**: This is by design — the worker uses `gh` CLI to discover new runs. But it means the worker spends extra tokens/time discovering the real failure. The prompt could be improved.

**Where**: `services/langgraph/src/consumers/_ci_gate.py:_build_ci_fix_prompt()`

---

### Issue 6: Agent pushes multiple fix commits without CI feedback (MEDIUM)

**Symptom**: Inside the `--resume` session, Claude Code pushed 4 fix commits in rapid succession, each triggering a new CI run. It didn't wait for CI results between pushes — it read `gh run view` logs from the original failure and iteratively fixed issues it found.

**Impact**: 4 separate CI runs triggered when a single "fix all lint issues" commit would suffice. Wastes CI minutes.

**Root cause**: The CI fix prompt tells the worker to "fix the root cause, run local checks, commit and push" but doesn't tell it to fix ALL lint issues at once. The worker fixes one issue, pushes, checks CI, sees another issue, fixes, pushes...

**Fix**: Improve CI fix prompt to instruct: "Run `make lint` locally first, fix ALL issues, then push once."

**Where**: `services/langgraph/src/consumers/_ci_gate.py:_build_ci_fix_prompt()`

---

## Answers to Specific Questions

### Q: Does the agent commit or also push?
**Both.** Claude Code inside the worker does `git add`, `git commit`, AND `git push`. The CI gate does NOT push — it only monitors CI results after the worker pushes. The worker-wrapper extracts `commit_sha` from git after Claude Code finishes.

### Q: How are CI logs passed to the worker?
**Hybrid approach:**
1. CI gate extracts a **summary** (job name + failed step name, ≤500 chars) and passes it in the prompt
2. CI gate includes the **run URL** in the prompt
3. Worker is instructed to use `gh run view <run-id> --log` for full logs
4. Worker has `GITHUB_TOKEN` in env so `gh` CLI works

The worker reads full logs itself — the CI gate doesn't pass full log content.

### Q: Why did integration tests fail — who should have set TELEGRAM_BOT_TOKEN?
**The service-template is responsible.** It generates `.env.test` for integration tests, but that file doesn't include a dummy `TELEGRAM_BOT_TOKEN` with valid format. The bot initializes at lifespan and aiogram validates token format. Fix needed in `.env.test.jinja` to include a fake-but-valid-format token.

### Q: Why did Docker registry fail?
**Registry secrets (`REGISTRY_USER`, `REGISTRY_PASSWORD`) are empty on the GitHub repo.** The scaffolder sets them from orchestrator env vars during scaffold. Either the orchestrator's `.env` has empty values, or the secret-setting step silently failed. Needs investigation of scaffolder logs and `.env`.

### Q: Did the task correctly transition to `failed`?
**Yes, but then it entered an infinite loop.** The `ci_gate_failed` correctly set task to `failed`. But a supervisor automatically moves failed tasks back to `backlog → todo`, causing the dispatcher to re-dispatch the same already-implemented task. Worker finds nothing to commit → fails → supervisor retries → loop.

---

### Issue 7: PO silently ignores reminders when story is "in_progress" (HIGH)

**Symptom**: PO reminder fires at 16:25 and 16:37. PO checks story/tasks, but `response_empty=True` — user gets no update.

**Root cause**: PO's system prompt says "output nothing for system events needing no user attention". PO calls `get_story()` + `get_tasks()`, sees story `in_progress` and tasks `in_dev`, and decides "no news". It doesn't realize:
- The task has been through `failed → backlog → todo → in_dev` cycle 3+ times
- The supervisor moves tasks back so fast that PO never catches them in `failed` state
- API returns current status only, not transition history

**Impact**: User never gets proactive updates about progress OR failures. The whole point of the reminder is to check in with the user, but PO stays silent.

**Fix options**:
1. Change PO prompt: on reminder, ALWAYS notify user with current status (even "still working")
2. Enrich `get_tasks()` response with failure count / recent event history so PO can see trouble
3. Add a `get_story_health()` tool that returns a summary including retry counts and time-in-progress

**Where**: `services/langgraph/src/prompts/po/`, `services/langgraph/src/agents/po/tools.py`

---

## Action Items

| # | Priority | Issue | Owner | Effort |
|---|----------|-------|-------|--------|
| 1 | 🔴 CRITICAL | Fix infinite retry loop — add terminal failure state for infra issues | orchestrator | M |
| 2 | 🔴 CRITICAL | Fix pre-push hook native fallback — use `make lint` or activate .venv | service-template | S |
| 3 | 🟠 HIGH | Add dummy TELEGRAM_BOT_TOKEN to `.env.test.jinja` | service-template | S |
| 4 | 🟠 HIGH | Investigate & fix registry secrets not being set on repos | orchestrator/scaffolder | S |
| 5 | 🟠 HIGH | PO should always notify user on reminder (not silently re-schedule) | orchestrator | S |
| 6 | 🟡 MEDIUM | Improve CI fix prompt — "fix ALL issues, push once" | orchestrator | S |
| 7 | 🟢 LOW | Include latest CI run URL in fix prompt (not just original) | orchestrator | S |

---

## Git Commits in reverse-bot

| SHA | Message | CI Result |
|-----|---------|-----------|
| `489f73d` | Initial commit | failure (expected — scaffold only) |
| `70ff6cf` | feat: scaffold reverse-bot with modules: backend | failure (scaffold, no app code) |
| `bb56f0f` | feat: add Telegram bot handler that reverses text messages | failure (lint) |
| `80e3567` | fix: configure deptry to ignore known false-positive dependency warnings | failure (lint) |
| `e3627ff` | fix: gracefully skip bot startup when TELEGRAM_BOT_TOKEN is not set | failure (lint — xenon) |
| `8e0b3cc` | fix: reduce lifespan complexity to satisfy xenon max-absolute B | failure (integration tests — token) |
| `1f41cd6` | fix: use empty TELEGRAM_BOT_TOKEN in .env.example for CI compatibility | failure (build-and-push — registry) |

**Lint passed on 5th commit. Integration tests passed on 5th commit. Build-and-push never passed (registry auth).**

---

## Resource Usage

- **Worker container**: `dev-reverse-bot-7d30d0f2`, up 22+ minutes (still running in infinite loop)
- **CI runs**: 7+ GitHub Actions runs (5 from pushes + 1 rerun + loop runs)
- **Claude Code sessions**: 1 initial + resumed for CI fix + at least 2 loop iterations
- **Ports allocated**: backend:8000, tg_bot:8001 on vps-267180
