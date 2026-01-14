# Current Implementation Gaps: Worker Manager

**Status:** ✅ P1.9 Integration Tests: 5/5 Passing — Phase 1 COMPLETE
**Last Updated:** 2026-01-13
**Related Specs:** [worker_manager.md](./services/worker_manager.md), [CONTRACTS.md](./CONTRACTS.md)

---

## Executive Summary

**Phase 1 complete.** All integration tests passing. Unit tests pass (46/46).

**Integration Test Results:**
| Test | Status |
|------|--------|
| test_backend_integration_smoke | ✅ PASSED |
| test_create_claude_worker_with_git_capability | ✅ PASSED |
| test_create_factory_worker_with_curl_capability | ✅ PASSED |
| test_different_agent_types_produce_different_images | ✅ PASSED |
| test_worker_executes_task_with_mocked_claude | ✅ PASSED |

**Ready to proceed to Phase 2.**

---

## Part 1: Resolved Issues (P1.9)

All original 7 blockers + 1 new have been fixed:

| # | Issue | Fix | Files Changed |
|---|-------|-----|---------------|
| 1 | Env var naming (`REDIS_URL` vs `WORKER_REDIS_URL`) | Added `WORKER_` prefix | `container_config.py` |
| 2 | Same images for different agents | Added `LABEL com.codegen.agent_type` | `image_builder.py` |
| 3 | Network isolation in DIND | Added `network_name` parameter | `manager.py`, `config.py`, `backend.yml` |
| 4 | Container name conflicts | Added `cleanup_worker_containers` fixture | `conftest.py` |
| 5 | Missing shared dependency | Added to pyproject.toml | `worker-wrapper/pyproject.toml` |
| 6 | Entrypoint fails | Fixed via #1 | (same as #1) |
| 7 | Image hash confusion | Documented; agent_type now in Dockerfile | `image_builder.py` |
| 8 | **Instruction injection broken** | **Used base64 encoding instead of shell quoting** | `manager.py` |

**Issue #8 Details:**
- Shell quoting with `shlex.quote()` produced nested single quotes: `sh -c 'echo 'content' > /path'`
- Fix: Use base64 + python decoder: `python3 -c "import base64; open(...).write(base64.b64decode(...).decode())"`

**Verification:** `make test-worker-manager-unit` passes (46/46). Integration tests pass (4/4).

| 9 | Image Hash Uniqueness | Verified `agent_type` included in hash calculation | `image_builder.py` |
| 10 | Lifecycle Events | Verified `wrapper.py` publishes events; consumer receives commands | `wrapper.py`, `consumer.py` |
| 11 | **Task Context Missing (Gap B)** | Added `task_id`/`request_id` to `worker:status:{id}` in wrapper | `wrapper.py` |

---

## Part 1.5: Previous Integration Test Blockers (RESOLVED)

All blockers from Phase 1 have been resolved:

1. ~~Image Hash Same for Different Agent Types~~ — Fixed: test now checks tags, not image ID
2. ~~Lifecycle Events Not Published~~ — Fixed: wrapper correctly publishes to `worker:lifecycle`

---

## Part 2: New Gaps (Spec vs Implementation)

### Gap A: Resource Limits NOT Implemented (CRITICAL)

**Spec Reference:** [worker_manager.md §6](./services/worker_manager.md#6-resource-management--quotas)

**What spec says:**
- `MAX_CONCURRENT_WORKERS` (default 10) — hard limit on active containers
- `WORKER_MEMORY_LIMIT` (default 512m) — per-container memory
- `WORKER_CPU_LIMIT` (default 0.5) — per-container CPU
- Creation queue when limit reached

**Current state:** Not implemented. Unlimited workers can be created.

**Risk:** Production system can be overloaded; no protection against resource exhaustion.

**Priority:** HIGH for production, can skip for MVP testing.

**Fix scope:** ~50 lines in `manager.py` + new settings in `config.py`

---


### Gap C: Developer Queue Routing Differs (MEDIUM)

**Spec Reference:** [CONTRACTS.md - Worker I/O](./CONTRACTS.md#worker-io)

| Aspect | Spec | Implementation |
|--------|------|----------------|
| Input Queue | `worker:developer:input` (shared) | `worker:{worker_id}:input` (per-worker) |
| Output Queue | `worker:developer:output` (shared) | `worker:{worker_id}:output` (per-worker) |

**Spec rationale:**
- Developer workers are ephemeral (per-task)
- LangGraph doesn't need to know worker_id
- Single consumer for all developer results
- Correlation via `task_id` in message

**Current implementation rationale:**
- Consistent with PO worker pattern
- Simpler routing (message goes directly to worker)
- Worker-manager doesn't need to route

**Decision needed:** Which approach?

**Recommendation:** Keep current (per-worker) for now. Simpler, works. Can migrate later if needed.

---

### Gap D: Idle Pause/Wakeup NOT Implemented (LOW)

**Spec Reference:** [worker_manager.md §6.3](./services/worker_manager.md#63-idle-pause-mechanism)

**What spec says:**
- Pause workers after `IDLE_TIMEOUT_SECONDS` (default 10m)
- Unpause when new message arrives
- Background task monitors activity

**Current state:** Not implemented.

**Impact:** Long-running PO workers consume resources even when idle.

**Priority:** LOW — paused containers still hold RAM. Better to terminate after session timeout.

**Recommendation:** Skip for MVP. Use session rotation (terminate after 30m idle) instead.

---

### Gap E: Creation Queue NOT Implemented (LOW)

**Spec Reference:** [worker_manager.md §6.1](./services/worker_manager.md#61-global-concurrency-limits)

**What spec says:**
- When `MAX_CONCURRENT` reached, queue request in `worker:creation_queue`
- Background consumer processes when slot opens
- Timeout after `WORKER_CREATION_TIMEOUT`

**Current state:** Not implemented.

**Impact:** If limits are added (Gap A), requests will be rejected instead of queued.

**Priority:** LOW — reject is simpler and acceptable for MVP.

**Recommendation:** Skip. Implement if user feedback demands queueing.

---

## Part 3: Spec Decisions (Keep Current)

These items differ from spec but current implementation is BETTER:

### 1. GIT/CURL Pre-installed

| Spec | Current |
|------|---------|
| Install via `apt-get` at build time | Pre-installed in worker-base |

**Why current is better:**
- All workers need git/curl
- Faster container startup
- Simpler Dockerfile generation
- Less build time

**Status:** Keep current.

### 2. Instructions File Location

| Spec | Current |
|------|---------|
| `/app/CLAUDE.md` | `/workspace/CLAUDE.md` |

**Why current is better:**
- `/workspace` is agent's working directory
- Agent naturally looks there for instructions
- Consistent with copier template output

**Status:** Keep current.

### 3. Env Var Naming with WORKER_ Prefix

| Spec | Current |
|------|---------|
| `REDIS_URL`, `AGENT_TYPE` | `WORKER_REDIS_URL`, `WORKER_AGENT_TYPE` |

**Why current is better:**
- Compatible with pydantic-settings `env_prefix`
- Clear namespace separation
- Avoids conflicts with system vars

**Status:** Keep current. Update spec.

### 4. Heartbeat Mechanism

| Spec | Current |
|------|---------|
| Optional `SETEX worker:heartbeat:{id}` | Not implemented |

**Why skip:**
- Docker Events API sufficient for death detection
- Heartbeat adds complexity
- No clear benefit over events

**Status:** Skip permanently.

---

## Part 4: Test Coverage Gaps

From [tests/services/worker_manager.md](./tests/services/worker_manager.md):

| Scenario | Spec | Status |
|----------|------|--------|
| A: Lifecycle | Basic spawn/terminate | Partial |
| B: Cache Hit/Miss | Image caching verification | Partial |
| C: Garbage Collection | LRU cleanup | Not implemented |
| D: Configuration Verification | Check CLAUDE.md injected | Not implemented |
| E: Mock LLM Integration | `<result>` parsing | Not implemented |
| F: Status Lifecycle | Redis state transitions | Partial |
| G: Graceful Deletion | Manual stop | Partial |
| H: Idle Pause | Pause/unpause | Skip (see Gap D) |
| I: Auto-Wakeup | Resume on message | Skip (see Gap D) |
| J: Resource Limits | MAX_CONCURRENT | Skip until Gap A fixed |

**Priority tests to add:**
1. Scenario D: Verify CLAUDE.md/AGENTS.md exists in container
2. Scenario E: Mock LLM returns `<result>`, verify parsing

---

## Part 5: Action Items

### For P1.9 Completion (Integration Tests Pass)

- [x] Fix env var naming
- [x] Fix network isolation
- [x] Add test cleanup
- [x] Add shared dependency
- [ ] Run integration tests and verify

### For Production Readiness

| Item | Priority | Effort |
|------|----------|--------|
| Gap A: Resource Limits | HIGH | Medium (~50 lines) |
| Gap B: Task Context | HIGH | Small (~20 lines) |
| Scenario D/E tests | MEDIUM | Medium |

### Deferred (Post-MVP)

- Gap C: Developer queue routing (works, just different)
- Gap D: Idle pause/wakeup
- Gap E: Creation queue
- Scenario C: GC tests

---

## Part 6: P1.10 Blocking Tests (NEW)

**Spec:** [tests/P1_BLOCKING_TESTS.md](./tests/P1_BLOCKING_TESTS.md)

Текущие интеграционные тесты (P1.9) проверяют только базовую функциональность:
- Контейнер создаётся ✅
- Git/curl установлены ✅
- CLAUDE.md инжектится ✅

**НЕ проверяется:**
- Claude CLI / Droid CLI установлены ❌
- orchestrator-cli работает внутри контейнера ❌
- Auth (host session / API key) настроен ❌
- Реальное взаимодействие с LLM ❌

### Gaps to Fix for P1.10

| # | Gap | File | Fix |
|---|-----|------|-----|
| 1 | Agent CLI not installed | `image_builder.py` | Call `agent.get_install_commands()` |
| 2 | Node.js missing | `worker-base/Dockerfile` | Add `nodejs npm` to apt-get |
| 3 | Auth fields missing | `contracts/worker.py` | Add `auth_mode`, `host_claude_dir` |
| 4 | Auth not passed | `consumer.py` | Forward auth config to manager |
| 5 | FACTORY_API_KEY missing | `container_config.py` | Add to env vars |
| 6 | --dangerously-skip-permissions | `runners/claude.py` | Add flag to command |

### Test Series

- **Series 1:** Claude Agent (7 tests) — installation, auth, CLI
- **Series 2:** Factory Agent (7 tests) — installation, auth, CLI
- **Series 3:** E2E Real LLM (4 tests) — skipped by default, requires real keys

**Phase 2 blocked until all P1.10 tests pass.**

---

## Related Documents

- [worker_manager.md](./services/worker_manager.md) — Service specification
- [CONTRACTS.md](./CONTRACTS.md) — Queue contracts
- [tests/services/worker_manager.md](./tests/services/worker_manager.md) — Test scenarios
- [tests/P1_BLOCKING_TESTS.md](./tests/P1_BLOCKING_TESTS.md) — **P1.10 blocking tests specification**
- [MIGRATION_PLAN.md](./MIGRATION_PLAN.md) — Phase 1 milestones
- [STATUS.md](../STATUS.md) — Project status
