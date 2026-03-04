# Plan: Fix & Consolidate Test Suites (#6)

## Context

Task #6 is partially done. Completed: Makefile cleanup, placeholder stubs removed, service tests working, TESTING.md updated (basic). Remaining:

1. Implement `test_langgraph_integration.py` — real integration tests using the full backend compose stack
2. Final pass on `docs/TESTING.md` after stabilization

The skeleton exists at `tests/integration/backend/test_langgraph_integration.py` with two stub tests. The backend integration suite (`docker/test/integration/backend.yml`) runs all services: langgraph, worker-manager, api, **db (PostgreSQL)**, redis, DinD.

**Approach**: Full-stack integration — tests run against real services (DB, Redis, API, LangGraph, worker-manager). Data is seeded via API endpoints. Only truly external services (GitHub API, LLM APIs) are out of scope — the tests exercise the flow up to the boundary where those external calls would happen.

This matches how the existing `test_worker_execution.py` tests work: they publish real Redis messages, hit real worker-manager, create real containers in DinD. The langgraph tests follow the same pattern but enter through the engineering queue.

**Key insight**: The engineering worker flow has clear internal boundaries we can test:
1. Queue consumer → API (project fetch, task status update) → DB — **testable**
2. Resource allocator → API (server fetch, allocation create) → DB — **testable**
3. Developer node → worker spawner → Redis → worker-manager → DinD — **testable** (existing tests cover worker creation; we add the langgraph→Redis trigger)
4. GitHub API calls, CI gate, LLM execution — **external, out of scope**

**Helper duplication**: `wait_for_stream_message` and `wait_for_create_response` are defined directly in `test_worker_execution.py`. Consolidate into conftest so `test_langgraph_integration.py` can reuse them.

## Steps

1. [x] Consolidate duplicated test helpers into conftest
   - **Input**: `tests/integration/backend/test_worker_execution.py`, `conftest.py`
   - **Output**: `wait_for_stream_message`, `wait_for_create_response` moved to `conftest.py`; `test_worker_execution.py` imports from conftest
   - **Test**: `make test-integration-backend` — existing tests still pass

2. [x] Add API client fixture + data seeding helpers to conftest
   - **Input**: `tests/integration/backend/conftest.py`
   - **Output**: New fixtures:
     - `api_client` — `httpx.AsyncClient` pointing at `http://api:8000` (or `172.31.0.20:8000`)
     - `seed_project(id, name, status, config, repository_url)` — creates project via `POST /api/projects/`
     - `seed_task(id, type, project_id)` — creates task via `POST /api/tasks/`
     - `seed_server(handle, host, public_ip, status, capacity_ram_mb)` — creates server via `POST /api/servers/`
     - Autouse cleanup fixture that deletes seeded records after each test (or relies on tmpfs DB reset per session)
   - **Test**: write a quick smoke test that seeds a project and reads it back via API

3. [x] Implement langgraph integration tests (real DB, real Redis, real API)
   - **Input**: `tests/integration/backend/test_langgraph_integration.py` (skeleton), engineering worker code
   - **Output**: 3 test scenarios against the full stack:

     **a) `test_engineering_worker_processes_queue_and_updates_task`**
     - Seed: project (status=`draft`, action=`create`), task (status=`queued`), server (status=`ready`, ram=8192)
     - Action: queue `EngineeringMessage` to `engineering:queue`
     - Assert: poll API until task status changes from `queued` → `running` (engineering worker picked it up)
     - Assert: the flow eventually fails at GitHub boundary (`_create_repo_and_set_secrets` fails because `GITHUB_ORG`/GitHub App creds aren't configured in test env)
     - Assert: task status becomes `failed` with an error message mentioning GitHub/repo
     - **What this tests**: Redis consumer → API project fetch → task status lifecycle → error propagation — all through real services

     **b) `test_engineering_worker_missing_project_fails_task`**
     - Seed: task only (no project in DB)
     - Action: queue `EngineeringMessage` with non-existent `project_id`
     - Assert: task status becomes `failed` with "not found" error
     - **What this tests**: error handling path with real DB — worker queries API, project doesn't exist, task marked failed

     **c) `test_engineering_worker_scaffold_failed_aborts`**
     - Seed: project (status=`scaffold_failed`), task
     - Action: queue `EngineeringMessage`
     - Assert: task status becomes `failed` with error mentioning "scaffold_failed"
     - **What this tests**: fail-fast guard with real DB state

   - **Test**: `make test-integration-backend` passes with new tests

4. [x] Update TESTING.md — final pass
   - **Input**: `docs/TESTING.md`, current test state
   - **Output**: Updated coverage matrix (langgraph: stub → 3 real integration tests), accurate descriptions, verify Makefile targets listed are correct, document the seeding approach (API fixtures)
   - **Test**: —

5. [x] Update backlog and STATUS.md
   - **Input**: `docs/backlog.md`, `docs/STATUS.md`
   - **Output**: #6 moved to Done, STATUS.md cleared, CHANGELOG updated
   - **Test**: —

## Notes

- The `langgraph` service in the compose already connects to `redis` and `api`. The engineering worker runs inside it. We just need to ensure `engineering:queue` consumer group exists (the service creates it on startup).
- API auto-runs `alembic upgrade head` on startup — DB schema is ready.
- DB uses `tmpfs` — data is ephemeral per compose session. No cleanup needed between sessions, but we should clean up between tests within a session (delete seeded records or use unique IDs per test).
- The test runner container already has `shared/` and `services/` mounted, so it can import contracts (`EngineeringMessage`, etc.).
- Environment: `GITHUB_ORG`, `GITHUB_APP_ID`, etc. are NOT set in the test compose — the engineering worker will fail at the GitHub boundary naturally. This is the expected behavior for these tests.

## Deviations

- **Step 1**: Also consolidated helpers from `test_task_injection.py` (not just `test_worker_execution.py` as planned).
- **Step 2**: Cleanup uses `DELETE /api/projects/{id}` cascade (deletes tasks + allocations) instead of autouse fixture. No server cleanup (no DELETE endpoint, but DB is tmpfs).
- **Step 3**: Added `engineering-worker` service to `backend.yml` — it was missing from the test compose. The `langgraph` container only runs PO/provisioner, not the engineering queue consumer. Task for "missing project" test seeded without `project_id` (FK constraint prevents referencing non-existent project).
- **CI fix 1**: Added `__init__.py` to `tests/`, `tests/integration/`, `tests/integration/backend/` — relative imports (`from .conftest import`) require the directory to be a Python package.
- **CI fix 2**: Removed custom `command` override on API service in test compose — it was skipping `entrypoint.sh` which runs `alembic upgrade head`. Also added `SECRETS_ENCRYPTION_KEY` env var.
