# Decouple shared/ from Docker builds — reduce rebuild blast radius

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

`shared/` is a monolithic package (11 submodules) installed via `pip install ./shared` in every Dockerfile. Any change to ANY file in shared/ triggers rebuild of ALL 8 service Docker images and ALL worker base images (~5min total). The task description recommends combining approach A (narrow WORKER_SOURCE_HASH) + B (drop pip install, use plain COPY + PYTHONPATH).

**Current state:**
- Service Dockerfiles: multi-stage, extract deps from shared/pyproject.toml, install deps first (cached), then `pip install --no-deps ./shared`
- Worker base: builds wheels for shared + worker-wrapper + orchestrator-cli in builder stage
- WORKER_SOURCE_HASH: hashes ALL files in shared/ + packages/ + worker Dockerfiles
- docker-compose: all services mount `./shared:/app/shared:delegated` for dev hot-reload (already uses PYTHONPATH effectively)
- worker-wrapper only imports: `shared.log_config.config`, `shared.redis.client`
- orchestrator-cli imports: `shared.contracts.dto`, `shared.crypto`, `shared.queues`, `shared.config`

**Key insight:** Dev mode already works via volume mount + PYTHONPATH. The pip install is only needed for Docker image builds. Since shared is pure Python with no C extensions, plain COPY is equivalent.

## Steps

1. [ ] Narrow WORKER_SOURCE_HASH to only hash files workers actually use
   - **Input**: `Makefile` (lines 13-17)
   - **Output**: WORKER_SOURCE_HASH only hashes `shared/log_config/`, `shared/redis/`, `shared/redis_client.py`, `shared/__init__.py`, `shared/config.py`, `shared/queues.py`, `shared/contracts/`, `shared/crypto.py`, `packages/`, `services/worker-manager/images/`
   - **Test**: `make test-unit` passes; manually verify hash changes when touching shared/redis_client.py but NOT when touching shared/models/

2. [ ] Convert service Dockerfiles to plain COPY + PYTHONPATH (drop pip install shared)
   - **Input**: `services/api/Dockerfile`, `services/langgraph/Dockerfile`, `services/scheduler/Dockerfile`, `services/telegram_bot/Dockerfile`, `services/infra-service/Dockerfile`, `services/worker-manager/Dockerfile`
   - **Output**: Each Dockerfile replaces the shared dep-extraction + pip install pattern with simple `COPY shared ./shared` + `ENV PYTHONPATH=/app`. shared's pip deps (pydantic-settings, redis, structlog, etc.) are added to each service's own requirements.lock.
   - **Test**: `make build` succeeds; `make test-unit` passes; `make test-integration` passes

3. [ ] Move shared's pip dependencies into each service's requirements
   - **Input**: `shared/pyproject.toml` (dependencies list), each service's `pyproject.toml` or `requirements.lock`
   - **Output**: Each service lists the shared deps it actually needs (based on import map). shared/pyproject.toml keeps deps for local dev only. Services that don't use redis don't install redis, etc.
   - **Test**: `make build` succeeds; each service container starts and can import its shared modules

4. [ ] Simplify worker-base-common Dockerfile to use plain COPY for shared
   - **Input**: `services/worker-manager/images/worker-base-common/Dockerfile`
   - **Output**: Drop the shared wheel-building step. Use `COPY shared ./shared` + `PYTHONPATH=/app`. Keep wheel builds for worker-wrapper and orchestrator-cli (they have entry points). Move shared's deps needed by worker-wrapper/cli into their own pyproject.toml deps.
   - **Test**: `make rebuild-worker-images` succeeds; worker containers can `import shared.log_config` and `import shared.redis.client`

5. [ ] Add .dockerignore exclusions for shared submodules not needed per service
   - **Input**: Dependency matrix from task description, each service's Dockerfile
   - **Output**: Per-service `.dockerignore` or Dockerfile-level exclusion of unused shared submodules (e.g., api doesn't need shared/schemas/worker_events.py, telegram doesn't need shared/models/). This prevents COPY invalidation from irrelevant changes.
   - **Test**: Change a file in shared/models/; verify telegram_bot image layer cache is NOT invalidated

6. [ ] Integration test — full rebuild + smoke test
   - **Input**: All modified files from steps 1-5
   - **Output**: `make nuke` succeeds, all services start, basic health checks pass
   - **Test**: `make nuke && make test-all`


