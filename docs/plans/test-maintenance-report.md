# Test Maintenance Report — 2026-03-19

First run (last_commit was null). Full codebase review.

## Summary
- Integration suites run: 5/5
- Service suites run: 1 (api)
- Tests passed: 91 (14+1+6+9+19+42)
- Tests fixed: 4
- Tests deleted: 0
- New tests written: 7 (DTO contract tests)

## Results by Suite

| Suite | Result | Tests |
|-------|--------|-------|
| template | **14/14 pass** | ✓ |
| frontend | **1/1 pass** | Only smoke test |
| infra | **6/6 pass** | ✓ |
| po-tools | **9/9 pass** | ✓ |
| backend | **19/27 pass, 8 fail** | Worker container creation failures (Category C) |
| api (service) | **42/42 pass** | Including 7 new DTO contract tests |

## Fixes Applied

1. **File**: `tests/integration/backend/conftest.py:327`
   - **Issue**: `_SKIP_DIRS` didn't include `.venv`, `.mypy_cache`, `.ruff_cache` — `_content_hash()` tried to read dangling symlinks in `shared/.venv/bin/python`
   - **Fix**: Added `.venv`, `.mypy_cache`, `.ruff_cache` to `_SKIP_DIRS`

2. **File**: `tests/integration/backend/conftest.py:374`
   - **Issue**: `shutil.copytree(shared_path, ...)` copied `.venv` with dangling symlinks, crashing Docker image build
   - **Fix**: Added `ignore=shutil.ignore_patterns(*_SKIP_DIRS)` to both copytree calls

3. **File**: `services/langgraph/tests/service/test_story_worker_registry.py:75`
   - **Issue**: Manual JSON construction missing `command: "delete"` field that `DeleteWorkerCommand` DTO includes
   - **Fix**: Added `"command": "delete"` to cmd_data dict

4. **File**: `services/langgraph/tests/service/conftest.py:23`
   - **Issue**: Hardcoded Redis key strings `"story:workers"`, `"worker:commands"` instead of `shared.queues` constants
   - **Fix**: Import and use `STORY_WORKERS_KEY`, `WORKER_COMMANDS` from `shared.queues`

## Code Fixes Applied

5. **File**: `services/langgraph/src/nodes/developer.py:347`
   - **Issue**: Returned `engineering_status: "developer_blocked"` — a value not in the documented enum (`"idle" | "working" | "done" | "blocked" | "worker_rejected"`)
   - **Fix**: Changed to `"blocked"` to align with documented states

6. **File**: `services/langgraph/src/subgraphs/engineering.py:115-120`
   - **Issue**: `BlockedNode.run()` unconditionally overwrote `engineering_status` to `"blocked"`, destroying specific statuses like `"worker_rejected"`. This made both `_handle_worker_blocked` and `_handle_worker_reject` handlers dead code in the consumer.
   - **Fix**: BlockedNode now preserves `engineering_status` when it's already `"blocked"` or `"worker_rejected"`, only overwriting for generic cases (e.g. `"done"` that arrived via errors fallback)

7. **File**: `services/langgraph/src/consumers/engineering.py:241`
   - **Issue**: Consumer checked for `"developer_blocked"` which was never reachable (BlockedNode overwrote it)
   - **Fix**: Changed to check for `"blocked"` to match the now-normalized status flow

## New Tests Added

1. **File**: `services/api/tests/service/test_dto_contracts.py::test_project_response_validates_as_dto`
   - **Covers**: ProjectDTO ↔ API response contract
   - **Suite**: service/api

2. **File**: `services/api/tests/service/test_dto_contracts.py::test_task_response_validates_as_dto`
   - **Covers**: TaskDTO ↔ API response contract
   - **Suite**: service/api

3. **File**: `services/api/tests/service/test_dto_contracts.py::test_task_event_response_validates_as_dto`
   - **Covers**: TaskEventDTO ↔ API response contract
   - **Suite**: service/api

4. **File**: `services/api/tests/service/test_dto_contracts.py::test_story_response_validates_as_dto`
   - **Covers**: StoryDTO ↔ API response contract
   - **Suite**: service/api

5. **File**: `services/api/tests/service/test_dto_contracts.py::test_repository_response_validates_as_dto`
   - **Covers**: RepositoryDTO ↔ API response contract
   - **Suite**: service/api

6. **File**: `services/api/tests/service/test_dto_contracts.py::test_server_response_validates_as_dto`
   - **Covers**: ServerDTO ↔ API response contract
   - **Suite**: service/api

7. **File**: `services/api/tests/service/test_dto_contracts.py::test_application_response_validates_as_dto`
   - **Covers**: ApplicationDTO ↔ API response contract
   - **Suite**: service/api

## Action Items

- [ ] **Backend worker container tests (8 failures)**: Worker creation via DinD returns 404 "No such container". Tests: test_dev_env (3), test_task_injection (2), test_worker_execution (3). Likely a worker-manager issue building/starting containers in DinD — needs investigation of base image build inside DinD. (Category C: significant infrastructure issue)
- [ ] **Refactor `engineering_status` to StrEnum**: Currently bare strings scattered across developer.py, engineering.py, BlockedNode, DoneNode, and engineering consumer. Should be a proper `EngineeringStatus(StrEnum)` to prevent status mismatches like the `"developer_blocked"` bug.
- [ ] **Frontend integration suite is minimal**: Only 1 smoke test (health check). No telegram bot handler tests. `conftest.py` and `test_handlers.py` never existed in git — the .pyc files were Docker artifacts.

## Coverage Gaps

Logic that should be tested but isn't yet:

- **QA flow** (deploy → qa:queue → SSH → Claude Code → pass/fail → story complete or fix task) — cross-service, needs new integration suite
- **PR merge polling** (scheduler → GitHub → deploy:queue) — cross-service, needs infra integration tests with GitHub mock
- **Deploy failure classifier** (CODE_FIX/RETRY/GIVE_UP routing) — needs langgraph service tests
- **Worker rejection pipeline** (reject_reason → worker_rejected → engineering consumer) — needs langgraph service tests
- **Feature branches** (story/{id} → PR → merge → deploy) — cross-service, needs backend integration tests
- **Health checker worker** (HTTP polling → metrics → incidents) — needs scheduler service tests
- **Prometheus parser** (node_exporter/cadvisor parsing → metrics extraction) — needs scheduler service tests
- **Worker-wrapper HTTP server** (localhost:9090 /complete, /failed, /blocker) — worker_wrapper integration suite exists but minimal
- **telegram_bot** — zero service tests (only conftest exists)
