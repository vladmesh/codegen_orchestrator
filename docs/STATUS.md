# Project Status

> **Current Phase**: Architecture Refactoring (Migration to 2.0)
> **Active Plan**: [MIGRATION_PLAN.md](./new_architecture/MIGRATION_PLAN.md)

## 🚀 Current Focus

**Phase 1: Integration Milestone (P1.9) - FIXES APPLIED**

All identified gaps have been implemented. Integration tests passed successfully (4/4).

**Fixes Applied:** [CURRENT_GAPS.md](./new_architecture/CURRENT_GAPS.md)

### Resolved Issues

1. **✅ Env var naming** - Added `WORKER_` prefix to container_config.py
2. **✅ Network isolation** - Added `network_name` parameter, configured in backend.yml
3. **✅ Capability mechanism** - Added `LABEL` for agent_type differentiation
4. **✅ Test cleanup** - Added `cleanup_worker_containers` fixture
5. **✅ Task Context** - Added `task_id` persistence for crash recovery (Gap B)

### Progress (Phase 0)

1. **✅ P0.1 Shared Contracts**
   - Пакет `shared/contracts` создан.
   - Все DTO и сообщения очередей реализованы.

2. **✅ P0.2 Shared Redis**
   - Пакет `shared/redis` создан.
   - Клиент поддерживает Pydantic DTO.
   - Добавлен `FakeRedisStreamClient` для тестов.

3. **✅ P0.3 Shared Logging**
   - Пакет `shared/logging` создан.
   - Настроен `structlog` (JSON/Console).
   - Поддержка `correlation_id`.

4. **✅ P0.4 GitHub Client**
   - GitHub App authentication (JWT).
   - Token caching и Rate Limiting.
   - Тесты с `respx` и `freezegun`.

5. **✅ P0.5 Test Infrastructure**
   - [x] Настроить 4-уровневую систему тестов (Unit/Service/Integration/E2E)
   - [x] Перенести legacy тесты в карантин
   - [x] Обновить Makefile и compose файлы

### Next Steps

6. **✅ P1.0 API Service Refactor**
   - [x] API is now pure DAL (no Redis/GitHub side effects)
   - [x] Service tests verify strict CRUD behavior
   - [x] GitHub/Redis clients removed from API routers

7. **✅ P1.1 Orchestrator CLI**
   - [x] Created `packages/orchestrator-cli`.
   - [x] Implemented `project create` with Dual-Write (API + Redis).
   - [x] Permissions System Implemented.
   - [x] Verified with Unit & Integration Tests.

### Next Steps

8. **✅ P1.2 Worker Wrapper**
   - [x] Package `packages/worker-wrapper` created.
   - [x] Main Loop implemented (XREAD -> Subprocess -> XADD).
   - [x] Lifecycle events and session management via `WorkerLifecycleEvent`.
   - [x] Verified with Integration Tests (FakeRedis).

9. **✅ P1.3 Infra Service**
   - [x] Renamed to `services/infra-service`.
   - [x] Implemented `provisioner:queue` consumer.
   - [x] Replaced functional Ansible usage with `AnsibleRunner` class.
   - [x] Removed legacy deployment logic.

### Next Steps

10. **✅ P1.4 Worker Manager**
    - [x] **P1.4.1 Core Logic**: `WorkerManager` class, `DockerClientWrapper`, Service Tests (Green).
    - [x] **P1.4.2 Service Wiring**: Wiring `main.py` with `WorkerCommandConsumer` (Redis), Background Tasks (GC, Auto-Pause).
    - [x] **P1.4.3 Worker Base**: Build `worker-base` image, CI integration.

11. **✅ P1.5 Runtime Cache**
    - [x] **P1.5.1 ImageBuilder**: Dockerfile generation logic, `compute_image_hash()`, capability mapping.
    - [x] **P1.5.2 Build Logic**: `DockerClientWrapper.build_image()`, `WorkerManager.ensure_or_build_image()`, `create_worker_with_capabilities()`.

12. **✅ P1.6 Agent Integration**
    - [x] **P1.6.1 Agent Factories**: `AgentConfig` base, Claude/Factory configs, agent type in hash.
    - [x] **P1.6.2 Container Config**: Env vars, volumes (Docker socket, Claude session), network.
    - [x] **P1.6.3 Docker Exec**: `exec_in_container()`, instruction file injection.
    - [x] **P1.6.4 Docker Events**: Crash detection, failure forwarding to output queue.

13. **✅ P1.7 Wrapper Completion**
    - [x] **P1.7.1 Headless Execution**: ClaudeRunner, FactoryRunner classes.
    - [x] **P1.7.2 Result Parsing**: `<result>...</result>` extraction, DTO mapping.
    - [x] **P1.7.3 Session Management**: Redis storage, TTL, `--resume` flag.

14. **✅ P1.8 Worker Base Image**
    - [x] **P1.8.1 Dockerfile Update**: worker-wrapper as ENTRYPOINT, healthcheck.
    - [x] **P1.8.2 CI Integration**: Build with wrapper, lifecycle event test.
    - [x] **P1.8.3 Optimization**: Introduced `worker-base-common` to deduplicate layers and reduce disk usage.

15. **✅ P1.9 Phase 1 Milestone**
    - [x] `test_create_claude_worker_with_git_capability` - PASSED
    - [x] `test_create_factory_worker_with_curl_capability` - PASSED
    - [x] `test_different_agent_types_produce_different_images` - PASSED
    - [x] `test_worker_executes_task_with_mocked_claude` - PASSED
    - [x] `test_backend_integration_smoke` - PASSED

16. **✅ P1.10 Agent & CLI Tests**
    - [x] Gap 1-6 Fixed (Node.js, Auth, CLI installation)
    - [x] Series 1: Claude Agent Integration - PASSED
    - [x] Series 2: Factory Agent Integration - PASSED
    - [x] Series 3: E2E Manually Verified

### Phase 2: Core Logic
17. **✅ P2.1 Scaffolder Refactoring**
    - [x] Refactored `services/scaffolder` (DTOs, shared client).
    - [x] Added Unit Tests (10) and Service Tests (4).
    - [x] Integrated with CI.

18. **✅ P2.2 LangGraph Service**
    - [x] `test_engineering_flow_happy_path` (Service Test) ✅
    - [x] `test_persistence_recovery` (Service Test) ✅
    - [x] `test_worker_crash_handling` (Service Test) ✅
    - [x] `test_deploy_flow_polling` (Service Test) ✅
    - [x] Implementation: `consumers.py`, `graph_runner.py`, `redis_publisher.py`

19. **✅ P2.3 Scheduler Refactoring**
    - [x] Separated Data Layer: Service now talks to API, not DB.
    - [x] Refactored Tasks: `github_sync` and `server_sync`.
    - [x] Verified: Unit Tests (Mock) + Service Tests (Real API).

20. **✅ P2.4 Provisioner Result Listener**
    - [x] Implemented `provisioner_result_listener.py` consumer task.
    - [x] Added `provisioner:results` consumer loop to scheduler `main.py`.
    - [x] Implemented full feedback loop (Infra -> Redis -> Scheduler -> API -> DB).
    - [x] Verified: Service Tests (5/5) & Integration Tests (2/2).

## 🔗 Quick Links

- [Migration Plan](./new_architecture/MIGRATION_PLAN.md)
- [**Current Gaps (Active)**](./new_architecture/CURRENT_GAPS.md)
- [Testing Strategy](./new_architecture/tests/TESTING_STRATEGY.md)
- [Legacy Backlog](./backlog.md)
