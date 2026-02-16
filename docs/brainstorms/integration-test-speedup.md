# Integration Test Speedup

## Current State

- 5 integration test suites: `backend`, `cli`, `infra`, `frontend`, `template`
- Each is a separate `docker compose` project (`docker/test/integration/*.yml`)
- `make test-integration` runs all sequentially
- Total time: ~10 minutes (will grow with new tests)

## Where Time Goes

Each suite independently:
1. Builds service images (`docker compose build`)
2. Starts infrastructure (db, redis, DIND) and waits for healthchecks
3. Runs tests

**Redundant work across suites:**

| Image | backend | cli | infra | frontend | template |
|-------|---------|-----|-------|----------|----------|
| api | build | build | build | build | - |
| langgraph | build | - | - | - | - |
| worker-manager | build | - | - | - | - |
| scheduler | - | - | build | - | - |
| telegram-bot | - | - | - | build | - |
| test-runner | build | build | build | build | build |
| redis | start | start | start | start | - |
| db | start | start | start | - | - |

`api` builds 4 times, `test-runner` builds 5 times, `redis` starts 4 times.

## Options

### 1. Parallel CI jobs (low effort, high impact)

Split `test-integration` into parallel matrix jobs, same pattern as unit tests.

```yaml
test-integration:
  strategy:
    fail-fast: false
    matrix:
      suite: [backend, cli, infra, frontend, template]
  steps:
    - run: make test-integration-${{ matrix.suite }}
```

**Impact:** 5 sequential ~2min jobs -> 5 parallel ~2min jobs = ~2-3 min total.
**Effort:** Small CI change. No code changes.
**Caveat:** More GitHub Actions runner minutes (same total compute, lower wall time).

### 2. Pre-build shared base image (medium effort, medium impact)

Create `docker/test/integration/Dockerfile.base`:
```dockerfile
FROM python:3.12-slim
COPY shared ./shared
COPY packages ./packages
RUN pip install ./shared ./packages/orchestrator-cli ./packages/worker-wrapper
```

Service Dockerfiles and test-runner inherit from it. One build instead of N repeated `pip install shared`.

**Impact:** Saves ~30-60s per suite (pip install overhead).
**Effort:** Refactor Dockerfiles, add build step.

### 3. Consolidate infrastructure (medium effort, high impact)

Instead of each suite spinning up its own db+redis:
- One shared infra compose (db, redis, api)
- Test runners connect to shared infra
- Run suites sequentially on shared stack (no teardown/rebuild between suites)

**Impact:** Eliminates ~15-20s healthcheck wait per suite, eliminates redundant image builds.
**Effort:** Refactor compose files, ensure test isolation (separate DBs or cleanup).
**Risk:** Test isolation — need to ensure suites don't interfere.

### 4. Local parallelism with make -j (no effort)

Already works thanks to unique compose project names (`$(TEST_PROJECT)_$*`):
```bash
make -j4 test-integration-backend test-integration-cli test-integration-infra test-integration-frontend
```

**Impact:** Same as option 1 but locally.
**Effort:** Zero — already supported.
**Caveat:** Needs enough CPU/RAM for parallel Docker builds.

## Recommendation

**Start with option 1** — matrix CI jobs. Biggest impact, smallest change. Then consider option 3 if test count grows significantly.

Option 4 is free for local dev — just document it.
