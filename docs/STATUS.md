# Project Status

> **Current Phase**: Phase 4: System E2E & Final Polish

## 🚀 Current Focus

**Phase 4: System E2E Integration**

We have completed the core migration to the new architecture (Worker Manager, LangGraph, Infra Service). The focus is now on end-to-end system validation and CI integration.

### ✅ Completed Milestones

#### Phase 0: Foundation
- **DTO Contracts**: `shared/contracts` implemented and verified.
- **Shared Redis**: `shared/redis` with Pydantic support.
- **Shared Logging**: `structlog` implementation.
- **Github Client**: Token caching, rate limiting.
- **Test Infra**: 4-layer test strategy (Unit/Service/Integration/Milestone).

#### Phase 1: Base Components (Worker Management)
- **API Refactor**: Pure DAL (no side effects).
- **Orchestrator CLI**: Dual-write (API + Redis), Permissions.
- **Worker Wrapper**: Headless agent execution, session management (Redis), result parsing.
- **Infra Service**: Ansible runner, provisioning queue consumer.
- **Worker Manager**:
  - Docker container lifecycle management.
  - Docker container lifecycle management.
  - Dynamic image building with caching (hash-based).
  - **Agent Integration**: Claude Code & Factory Droid support.
  - **Capabilities**: Custom worker capabilities (git, curl, docker).

#### Phase 2: Core Logic (Orchestration)
- **Scaffolder**: Copier integration, GitHub repo creation.
- **LangGraph**:
  - Engineering Subgraph (Scaffolder -> Developer -> Tester).
  - DevOps Subgraph (EnvAnalyzer -> Deployer).
  - State persistence (Postgres).
- **Scheduler**: Migrated to API client, Provisioner Result Listener loop.
- **Template Tests**: Validated service-template integration.

#### Phase 3: Access (Telegram Bot)
- **POSessionManager**: Redis streams integration.
- **User Interface**: Progress events, synchronous waiting mode.
- **Admin Tools**: Provisioning notifications.

#### Phase 4: E2E (Partial)
- **Mock Anthropic**: E2E tests with deterministic LLM responses (Passed).
- **Configurable Agents**: Support for switching default agent type via env var.
- **Legacy Removal**: Dead code cleanup (analyst, zavhoz, graph_runner, legacy tests).

### 🚧 Outstanding Tasks

#### System E2E (P4.1)
- [ ] Full system `docker-compose up` validation.
- [ ] User -> Bot -> PO -> Spec -> Dev -> Deploy -> URL flow.

#### CI/CD
- [ ] Update CI workflows to run full E2E suite.

## 🔗 Quick Links

- [Architecture](../ARCHITECTURE.md) — High-level system overview.
- [Testing Strategy](./TESTING.md) — How to run tests.
- [Contracts](./CONTRACTS.md) — Queue schemas and DTOs.
- [Audit](./audit.md) — Known issues and gaps.
