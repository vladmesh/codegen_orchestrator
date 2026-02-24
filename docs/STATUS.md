# Project Status

> **Current Focus**: Worker Reuse for CI Fix Loop
> **Plan**: [worker-reuse-ci-fix.md](plans/worker-reuse-ci-fix.md)

## Current: Worker Reuse for CI Fix Loop (#8)

При CI failure engineering-worker спавнит новый контейнер для каждого retry — теряется контекст агента и тратится ~30s на warmup. Переводим на reuse существующего контейнера: wrapper ждёт следующий input, spawner шлёт prompt напрямую.

Plan: [worker-reuse-ci-fix.md](plans/worker-reuse-ci-fix.md)

## Previous: Redis Streams Unification (#3+#5) — Done

Plan: [redis-streams-unification.md](plans/redis-streams-unification.md) — unified all 9 Redis Stream consumers to `RedisStreamClient.consume()` with PEL recovery, added Pydantic contracts to all queues.

## Previous: CI Fix Loop Improvements — Done

E2E iter 13 выявил каскадные проблемы в CI fix loop. Исправлено: project_id passthrough, CI контекст для девелопера, attempts tracking, обогащение сообщений. Investigation: [e2e-iter13-ci-fix-loop-deficits.md](investigations/e2e-iter13-ci-fix-loop-deficits.md).

## Previous: Native Dev Environment + Workspace Persistence — Done

Plans: [dev-env-architecture.md](plans/dev-env-architecture.md), [workspace-persistence.md](plans/workspace-persistence.md). Flat Dev Environment (bind-mounted workspaces, dual-network), workspace persistence по project_id. Phase 6 (failure counter) в backlog.

## Previous: PO ReactAgent Migration — Done

Plan: [po-react-agent.md](plans/po-react-agent.md) — fully implemented and merged.

## Previous: Deploy Architecture — Done

Plan: [deploy-architecture.md](plans/deploy-architecture.md) — 9 iterations: Fernet crypto, env groups, GitHub Actions deploy, self-hosted registry + Caddy, cascade failure fixes.

## Quick Links

- [Architecture](../ARCHITECTURE.md) — High-level system overview.
- [Testing Strategy](./TESTING.md) — How to run tests.
- [Contracts](./CONTRACTS.md) — Queue schemas and DTOs.
- [Audit](./audit.md) — Known issues and gaps.
