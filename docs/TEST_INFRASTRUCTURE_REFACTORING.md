# Test Infrastructure Refactoring Plan

> **Status:** Draft  
> **Created:** 2026-01-16  
> **Author:** Claude

---

## Current Problems

1. **Mixed test types** — `docker-compose.test.yml` used for unit, service, AND integration tests
2. **Missing compose files** — `test-scheduler-integration`, `test-api-integration` don't have proper configs
3. **Broken CI** — `make test-integration` calls non-existent or misconfigured targets
4. **Naming confusion** — unclear distinction between service and integration tests

---

## Target Architecture

### Test Levels

| Level | Location | Dependencies | Compose Location |
|-------|----------|--------------|------------------|
| Unit | `services/X/tests/unit/` | None (mocks only) | `docker/test/service/X.yml` with `--no-deps` |
| Service | `services/X/tests/service/` | Own DB/Redis | `docker/test/service/X.yml` |
| Integration | `tests/integration/` | Multiple services | `docker/test/integration/{test-name}.yml` |
| E2E | `tests/e2e/` | Full system + real APIs | Manual only |

### Key Principle: Integration Compose Per Test Group

Integration tests are **NOT** tied to a single service. Each integration test (or group) gets its own compose file based on what services it needs:

```
docker/test/integration/
├── backend.yml           # API + WorkerManager + LangGraph + Redis + DB
├── cli.yml               # API + Redis (for CLI tests)
├── frontend.yml          # API + LangGraph (for frontend flows)
└── scheduler-sync.yml    # API + Scheduler (for sync tests)
```

---

## Phase 1: Fix Immediate CI Failures

### 1.1 Fix LangGraph Integration Test Fixtures

**File:** `tests/integration/backend/test_langgraph_integration.py`

- [x] Rename `redis` → `redis_client` to match conftest

**File:** `tests/integration/backend/conftest.py`

- [x] Change `close()` → `aclose()` to fix deprecation warning

### 1.2 Remove Broken Targets from CI

**File:** `Makefile`

```makefile
# Current (broken):
test-integration: test-api-integration test-langgraph-integration test-scheduler-integration test-cli-integration

# Fixed (only existing targets):
test-integration: test-langgraph-integration test-cli-integration
```

**Reason:** `test-api-integration` and `test-scheduler-integration` don't have proper compose files.

---

## Phase 2: Standardize Service Tests

### 2.1 Create Missing Service Compose Files

Each service should have `docker/test/service/{service}.yml`:

| Service | Has Compose? | Action |
|---------|--------------|--------|
| api | ✅ Yes | Keep |
| langgraph | ✅ Yes | Keep |
| scheduler | ✅ Yes | Keep |
| scaffolder | ✅ Yes | Keep |
| worker-manager | ✅ Yes | Keep |
| infra | ✅ Yes | Keep |
| telegram | ❌ No | Create if needed |

### 2.2 Standardize Makefile Targets

Pattern for each service:

```makefile
test-{service}-unit:
    docker compose -f docker/test/service/{service}.yml run --rm --no-deps {service}-test-runner pytest tests/unit/ -v

test-{service}-service:
    docker compose -f docker/test/service/{service}.yml up --build --abort-on-container-exit
```

**Services to update:**
- [ ] `test-scheduler-unit` — currently uses `docker-compose.test.yml`
- [ ] Others already follow pattern

### 2.3 Deprecate docker-compose.test.yml

1. Migrate all targets to use `docker/test/service/*.yml`
2. Delete `docker-compose.test.yml`

---

## Phase 3: Reorganize Integration Tests

### 3.1 Current Integration Test Locations

```
tests/integration/
├── backend/                    # Uses docker/test/integration/backend.yml
│   ├── conftest.py
│   ├── test_claude_agent.py
│   ├── test_factory_agent.py
│   ├── test_langgraph_integration.py
│   ├── test_smoke.py
│   └── test_worker_execution.py
└── ...

services/scheduler/tests/service/   # WRONG LOCATION for integration tests!
├── test_github_sync_integration.py
└── test_server_sync_integration.py
```

### 3.2 Classify Scheduler "Service" Tests

Current `scheduler/tests/service/` tests are actually **integration tests** because they require the API service.

**Decision needed:**
- **Option A:** Keep as service tests, add API to `scheduler.yml`
- **Option B:** Move to `tests/integration/scheduler/`, create new compose

**Recommendation:** Option A — they test Scheduler's integration with API, which is its primary dependency.

### 3.3 Create Integration Compose Files

| Test Group | Services Needed | Compose File |
|------------|-----------------|--------------|
| Backend (workers) | API, WorkerManager, LangGraph, Redis, DB, Docker | `backend.yml` ✅ exists |
| CLI | API, Redis, DB | `cli.yml` ✅ exists |
| Scheduler Sync | API, Scheduler, Redis, DB | `scheduler.yml` (create) |

---

## Phase 4: Update CI Workflow

### 4.1 Current CI Structure

```yaml
jobs:
  test-unit:       # Per-service unit tests ✅
  test-integration: # Runs `make test-integration` ❌ broken
```

### 4.2 Target CI Structure

```yaml
jobs:
  test-unit:
    # Matrix of per-service unit tests
    run: make test-${{ matrix.service }}-unit

  test-service:
    # Matrix of per-service service tests
    run: make test-${{ matrix.service }}-service

  test-integration:
    # Individual integration test groups
    strategy:
      matrix:
        test: [backend, cli, scheduler-sync]
    run: make test-integration-${{ matrix.test }}
```

### 4.3 Updated Makefile Aggregators

```makefile
# All unit tests
test-unit: test-api-unit test-langgraph-unit test-scheduler-unit ...

# All service tests
test-service: test-api-service test-langgraph-service test-scheduler-service ...

# All integration tests
test-integration: test-integration-backend test-integration-cli
```

---

## Phase 5: Documentation & Cleanup

### 5.1 Update TESTING.md

Add section explaining:
- When to write unit vs service vs integration tests
- How to create a new integration test compose
- How to run tests locally

### 5.2 Remove Legacy Files

- [ ] `docker-compose.test.yml`
- [ ] Unused test targets in Makefile
- [ ] `tests_legacy/` directories (if empty)

### 5.3 Fix Deprecation Warnings

| Warning | File | Fix |
|---------|------|-----|
| `datetime.utcnow()` | `shared/tests/mocks/github.py` | Use `datetime.now(UTC)` |
| `close()` deprecated | `tests/integration/backend/conftest.py` | Use `aclose()` |

---

## Implementation Order

| Step | Task | Effort | Blocking |
|------|------|--------|----------|
| 1 | Fix fixture name in langgraph tests | 5 min | Yes (CI broken) |
| 2 | Remove broken targets from `test-integration` | 5 min | Yes (CI broken) |
| 3 | Update `test-scheduler-unit` to use proper compose | 15 min | No |
| 4 | Add API to `docker/test/service/scheduler.yml` | 10 min | No |
| 5 | Update CI to separate service/integration jobs | 30 min | No |
| 6 | Delete `docker-compose.test.yml` | 5 min | After step 3 |
| 7 | Fix deprecation warnings | 10 min | No |
| 8 | Update documentation | 20 min | No |

---

## Appendix: File Changes Summary

### New Files
- `docker/test/integration/scheduler.yml` (if Option B chosen)

### Modified Files
- `Makefile` — fix targets, remove legacy
- `.github/workflows/ci.yml` — add service tests job
- `tests/integration/backend/test_langgraph_integration.py` — fix fixture
- `tests/integration/backend/conftest.py` — fix deprecation
- `shared/tests/mocks/github.py` — fix deprecation
- `docker/test/service/scheduler.yml` — add API service

### Deleted Files
- `docker-compose.test.yml`
