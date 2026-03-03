# Plan: Fix & Consolidate Test Suites (#6)

## Context

Task #6 is partially done. Completed: Makefile cleanup, placeholder stubs removed, service tests working, TESTING.md updated (basic). Remaining:

1. Implement `test_langgraph_integration.py` — engineering-worker connectivity through Redis with mocked API/GitHub
2. Final pass on `docs/TESTING.md` after E2E stabilization

The skeleton exists at `tests/integration/backend/test_langgraph_integration.py` with two stub tests. The backend integration suite (`docker/test/integration/backend.yml`) runs all services: langgraph, worker-manager, api, scheduler, telegram_bot, redis, db, DinD.

**Challenge**: The engineering-worker runs in its own container. Mocking its internal deps (GitHub, API client) from the test runner is impractical. Two approaches:

- **A) In-process subgraph execution**: Import and run the engineering subgraph directly in the test runner, with real Redis + mocked HTTP clients. Tests LangGraph graph logic + Redis stream integration.
- **B) Full container test with mock HTTP server**: Spin up a mock GitHub/API server in the test network. More realistic but significantly more complex.

**Decision**: Approach A — in-process execution with real Redis. This matches the brief ("моки API/GitHub") and tests the important boundary: LangGraph ↔ Redis streams ↔ worker-manager. The engineering-worker container itself is already validated by E2E tests.

**Helper duplication**: `wait_for_stream_message` is copy-pasted between `test_worker_execution.py` and `test_task_injection.py`. Consolidate into conftest.

## Steps

1. [ ] Consolidate duplicated test helpers into conftest
   - **Input**: `tests/integration/backend/test_worker_execution.py`, `test_task_injection.py`, `conftest.py`
   - **Output**: `wait_for_stream_message`, `wait_for_create_response`, `cleanup_worker` moved to `conftest.py`; existing tests updated to use shared fixtures
   - **Test**: `make test-integration-backend` — existing tests still pass

2. [ ] Implement langgraph integration tests (in-process, mocked GitHub/API)
   - **Input**: `tests/integration/backend/test_langgraph_integration.py` (skeleton), `services/langgraph/src/subgraphs/engineering.py`, `services/langgraph/src/nodes/developer.py`, `services/langgraph/src/clients/worker_spawner.py`
   - **Output**: 3 test scenarios:
     - `test_engineering_subgraph_spawns_worker` — run engineering subgraph with mocked GitHub + API client, verify `CreateWorkerCommand` appears on `worker:commands` Redis stream
     - `test_engineering_subgraph_blocked_on_missing_project` — verify error handling when project not found
     - `test_worker_spawner_sends_create_command` — verify `WorkerSpawner.request_spawn()` publishes correct Redis message and reads response
   - **Test**: `make test-integration-backend` passes with new tests

3. [ ] Update TESTING.md — final pass
   - **Input**: `docs/TESTING.md`, current test state
   - **Output**: Updated coverage matrix (langgraph: skeleton → 3 tests), accurate date, verify all Makefile targets listed are correct, add note about in-process vs container integration test approach
   - **Test**: —

4. [ ] Update backlog and STATUS.md
   - **Input**: `docs/backlog.md`, `docs/STATUS.md`
   - **Output**: #6 moved to Done, STATUS.md cleared, CHANGELOG updated
   - **Test**: —
