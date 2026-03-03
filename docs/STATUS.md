# Project Status

> **Current Focus**: Worker Network Isolation (#22)
> **План**: [worker-network-isolation.md](plans/worker-network-isolation.md)

## Current: Worker Network Isolation (#22)

DNS-коллизия: воркер на `codegen_internal` видит postgres оркестратора вместо проекта. Решение — отдельная сеть `codegen_worker`, удаление хрупкого workaround `project-db`.

**Итерации**:
1. Создание сети `codegen_worker`, dual-homing bridge-сервисов
2. Удаление workaround (`project-db` alias, `_patch_db_hostname()`)
3. Тесты и валидация
4. Cleanup документации

## Previous work (summary)

| # | Задача | План |
|---|--------|------|
| 8 | Worker Reuse for CI Fix Loop | [worker-reuse-ci-fix.md](plans/worker-reuse-ci-fix.md) |
| 3+5 | Redis Streams Unification | [redis-streams-unification.md](plans/redis-streams-unification.md) |
| — | Native Dev Environment + Workspace Persistence | [dev-env-architecture.md](plans/dev-env-architecture.md), [workspace-persistence.md](plans/workspace-persistence.md) |
| — | PO ReactAgent Migration | [po-react-agent.md](plans/po-react-agent.md) |
| — | Deploy Architecture (9 iterations) | [deploy-architecture.md](plans/deploy-architecture.md) |

## Quick Links

- [Architecture](../ARCHITECTURE.md) — High-level system overview.
- [Testing Strategy](./TESTING.md) — How to run tests.
- [Contracts](./CONTRACTS.md) — Queue schemas and DTOs.
- [Audit](./audit.md) — Known issues and gaps.
