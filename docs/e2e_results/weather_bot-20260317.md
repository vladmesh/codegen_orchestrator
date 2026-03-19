# E2E Report: weather_bot — CI blocked by template test bugs

> **Date**: 2026-03-17
> **Project**: weather-bot (project_id: `796d95ad-d36f-4a9f-88b9-84b9401d9296`)
> **Story**: story-96d4d07a
> **Status**: Failed (CI blocked, no deploy)
> **Feature phase**: skipped
> **Smoke**: none
> **Worker reports**: collected (4)

---

## Timeline

```
19:05  Upsert test user, send project request to PO
19:05  PO asks for Telegram bot token
19:06  Token sent, PO confirms, creates story + submits to architect
19:06  Scaffold starts (architect waits 30s)
19:06  Scaffold complete, project DRAFT → ACTIVE
19:07  Architect creates 3 tasks (no blockers between them)
19:07  task-47ea214d dispatched → FAILED immediately (UUID type error)
19:09  [hotfix] Fix UUID→str in developer.py, rebuild engineering-worker
19:09  task-47ea214d retried (backlog → todo)
19:10  task-47ea214d in_dev (worker started)
19:16  task-47ea214d done (~6min)
19:16  task-74131537 in_dev
19:21  task-74131537 done (~4.5min)
19:21  task-5fc157e9 in_dev
19:25  task-5fc157e9 done (~4min)
19:26  All tasks done, dispatcher creates PR #1, story → pr_review
19:28  CI run #1 FAILED — ModuleNotFoundError: structlog
       Webhook did NOT fire (known issue for new repos)
       Scheduler only polls for merged PRs — no CI failure handling
20:39  [intervention] Story fail → reopen → in_progress, create fix task
20:40  task-4485f3d6 (fix structlog deps) dispatched
20:47  task-4485f3d6 done (~7min)
20:28  PR updated, CI run #2 starts
20:29  CI run #2 FAILED — tg_bot test_middleware.py: 2 test failures
       (TypeError: handler not BaseHandler, AssertionError: Application not initialized)
20:49  [abort] Test ended — CI still blocked by template test bugs
```

## PO Interaction

PO worked correctly. Created project, validated Telegram token, created story, submitted to architect. Asked relevant clarifying question (bot access model). No issues.

## Problems Found

### Problem 1: WorkerConfig.project_id receives UUID instead of str

- **Type**: orchestrator
- **Severity**: critical
- **Backlog**: new
- **Description**: `developer.py:230` passes `project_id` (a UUID object from `project_spec.get("id")`) directly to `request_spawn()`, which builds `WorkerConfig(project_id=...)`. Pydantic rejects UUID for a `str` field.
- **Root cause**: `project_spec` dict contains UUID objects (from SQLAlchemy model → Pydantic DTO → dict). `developer.py` doesn't cast to `str`.
- **Fix applied**: Added `str(project_id) if project_id else None` in `developer.py:230-231`. Engineering-worker rebuilt.
- **Suggested fix**: Also consider making `WorkerConfig.project_id` accept `str | UUID` or adding a validator.

### Problem 2: Webhook does not fire for newly scaffolded repos

- **Type**: orchestrator
- **Severity**: major
- **Backlog**: known issue (documented in skill)
- **Description**: After PR CI fails, no webhook arrives at the API. The scheduler's `complete_stories` only polls for merged PRs (`state=closed`). There is **no mechanism** to detect CI failure on an open PR without the webhook.
- **Root cause**: GitHub webhook may not be configured properly on newly created repos, or the webhook event type (`check_run` / `workflow_run`) is not set up.
- **Suggested fix**: Add a `pr_review` watcher to the scheduler that checks CI status on open PRs for stories in `pr_review`. If CI fails → create fix task + reopen story. This would make the pipeline self-healing without relying on webhooks.

### Problem 3: Workers push code that fails CI (no local test gate)

- **Type**: orchestrator
- **Severity**: major
- **Backlog**: new
- **Description**: All 3 feature workers reported pre-existing test failures (`test_middleware.py` — both backend and tg_bot) but pushed anyway. Worker 1 even reported `structlog` missing. Workers treat test failures as "pre-existing / unrelated" and push regardless.
- **Root cause**: Workers run `make lint` (which passes) but either skip tests entirely or ignore failures in tests they didn't write. There's no hard gate — INSTRUCTIONS.md apparently allows workers to push if they believe failures are pre-existing.
- **Suggested fix**: Workers should run `make tests` and only push if ALL tests pass, or at minimum if no NEW test failures were introduced. A pre-push hook or CI-local check would catch this before the code reaches GitHub.

### Problem 4: Generated tg_bot middleware tests are broken (template)

- **Type**: template
- **Severity**: major
- **Backlog**: template
- **Description**: `services/tg_bot/tests/unit/test_middleware.py` has 2 broken tests:
  1. `test_update_logged_with_standard_fields` — `TypeError: handler is not an instance of BaseHandler` (MagicMock doesn't satisfy type check)
  2. `test_handler_error_logged` — Application not initialized before `process_update()`
- **Root cause**: Template generates middleware tests that don't properly set up the Telegram `Application` object. `MagicMock()` is not a valid `BaseHandler` subclass.
- **Suggested fix**: Fix in `service-template` — either mock at the right level or properly initialize the Application in test fixtures.

### Problem 5: Generated backend middleware tests are broken (template)

- **Type**: template
- **Severity**: minor
- **Backlog**: template
- **Description**: `services/backend/tests/unit/test_middleware.py` — 4 tests fail because `log_client` fixture uses `@pytest.fixture` instead of `@pytest_asyncio.fixture`.
- **Root cause**: Template generates async fixture with wrong decorator.
- **Suggested fix**: Fix in `service-template` — use `@pytest_asyncio.fixture` for async generators.

### Problem 6: Lock files missing transitive dependencies from shared package

- **Type**: template
- **Severity**: major
- **Backlog**: template
- **Description**: `structlog` is declared in `shared/pyproject.toml` but was missing from service lock files. CI runs `uv sync --frozen` which actually *removed* structlog because it wasn't locked.
- **Root cause**: `make setup` / `uv lock` doesn't resolve shared package transitive deps into service lock files on initial scaffold.
- **Suggested fix**: Ensure `make setup` runs `uv lock` (not just `uv sync --frozen`) so transitive deps from `shared` are captured. Or add structlog explicitly to each service's deps.
