# Langfuse v3 infra — docker-compose + ClickHouse + nginx proxy

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Langfuse v3 self-hosted for LLM tracing — replaces LangSmith. Part of admin panel Phase 3 (brainstorm bs-2a1d6965).

**Current state**: No Langfuse or ClickHouse infrastructure exists. PostgreSQL runs single `orchestrator` DB, no init scripts. Redis has no auth. Admin-frontend nginx proxies /api/, /debug/, /grafana/, /wm-api/.

**Key finding from research**: Langfuse's `NEXT_PUBLIC_BASE_PATH` (for `/langfuse/` sub-path) requires **building langfuse-web from source** — prebuilt image doesn't support custom base path. This changes the original plan of nginx proxying at `/langfuse/*`.

**Decision**: Use prebuilt images, expose langfuse-web on port 3002 (host). Skip base path for now. Admin SPA (sibling task task-df069084) will link/iframe to `:3001/langfuse/` after we set up nginx proxy with a built image, or directly to `:3002`.

**MinIO**: Task says "S3/MinIO не нужен". Langfuse docs confirm core tracing works without S3. Skip for MVP — event uploads and media storage won't work but traces/observations via ClickHouse will.

Sibling tasks:
- task-300f55e6: LangChain → Langfuse tracing integration (env-var drop-in) — depends on this
- task-df069084: Admin SPA — LLM Tracing page (Langfuse iframe) — depends on this

## Steps

1. [ ] PostgreSQL init script — create `langfuse` database
   - **Input**: `docker-compose.yml` (db service), new file `infra/postgres/init-langfuse-db.sh`
   - **Output**: Shell script that creates `langfuse` database if not exists; mounted into `db` container at `/docker-entrypoint-initdb.d/`; db service in compose updated with volume mount
   - **Test**: `make nuke && make up` — verify `langfuse` database exists: `docker compose exec db psql -U postgres -c "\l" | grep langfuse`
   - ⚠️ Note: init scripts only run on fresh volumes. For existing deployments, add a manual migration note.

2. [ ] ClickHouse service in docker-compose
   - **Input**: `docker-compose.yml`, Langfuse official compose as reference
   - **Output**: `clickhouse` service: image `clickhouse/clickhouse-server`, user `101:101`, env vars (CLICKHOUSE_DB, USER, PASSWORD from .env), volumes (`clickhouse_data`, `clickhouse_logs`), healthcheck (`wget --spider http://localhost:8123/ping`), network `internal`, no host port. Timezone set to UTC.
   - **Test**: `docker compose up clickhouse -d && docker compose exec clickhouse wget -qO- http://localhost:8123/ping` returns "Ok"

3. [ ] Langfuse web + worker services in docker-compose
   - **Input**: `docker-compose.yml`, env vars for Langfuse
   - **Output**: Two services:
     - `langfuse-web`: image `langfuse/langfuse:3`, port `3002:3000`, depends on db, redis, clickhouse (healthy). Env: DATABASE_URL pointing to `db:5432/langfuse`, CLICKHOUSE_URL/MIGRATION_URL, REDIS_HOST=redis, REDIS_PORT=6379, REDIS_AUTH="" (no auth), NEXTAUTH_SECRET, SALT, ENCRYPTION_KEY from .env. All S3 vars omitted (MinIO skipped). Network: `internal`.
     - `langfuse-worker`: image `langfuse/langfuse-worker:3`, same env (YAML anchor), depends on same services. No host port. Network: `internal`.
   - **Test**: `docker compose up langfuse-web langfuse-worker -d && curl -sf http://localhost:3002` returns HTML. Check `docker compose logs langfuse-web` shows "Ready".

4. [ ] Environment variables in .env.example
   - **Input**: `.env.example`
   - **Output**: New section "Langfuse (LLM Tracing)" with:
     - `CLICKHOUSE_PASSWORD=change_me_in_production`
     - `LANGFUSE_NEXTAUTH_SECRET=` (generate instruction)
     - `LANGFUSE_SALT=` (generate instruction)
     - `LANGFUSE_ENCRYPTION_KEY=` (generate instruction via `openssl rand -hex 32`)
     - `LANGFUSE_INIT_USER_EMAIL=admin@localhost`
     - `LANGFUSE_INIT_USER_NAME=admin`
     - `LANGFUSE_INIT_USER_PASSWORD=change_me_in_production`
     - `LANGFUSE_INIT_ORG_NAME=codegen`
     - `LANGFUSE_INIT_PROJECT_NAME=orchestrator`
     - `LANGFUSE_INIT_PROJECT_PUBLIC_KEY=` (for integration task)
     - `LANGFUSE_INIT_PROJECT_SECRET_KEY=` (for integration task)
   - **Test**: `grep LANGFUSE .env.example` shows all vars. `grep CLICKHOUSE .env.example` shows password.

5. [ ] Nginx proxy for Langfuse (best-effort without base path)
   - **Input**: `services/admin-frontend/nginx.conf`
   - **Output**: Add `/langfuse/` location block proxying to `langfuse-web:3000`. Add `depends_on` for langfuse-web in admin-frontend compose service. Note: without `NEXT_PUBLIC_BASE_PATH`, internal links will break — this is acceptable for MVP since the iframe/link approach in sibling task will use port 3002 directly. The proxy serves as a convenience entry point only.
   - **Test**: `curl -sf http://localhost:3001/langfuse/` returns Langfuse HTML (may have broken asset paths — expected without base path build)

6. [ ] Seed script / make target for existing deployments
   - **Input**: `Makefile`
   - **Output**: `make init-langfuse-db` target that runs `docker compose exec db psql -U postgres -c "CREATE DATABASE langfuse"` (idempotent with IF NOT EXISTS). This handles existing deployments where init scripts won't re-run.
   - **Test**: Run twice — second run is a no-op.

7. [ ] Integration test — full stack smoke
   - **Input**: Running stack (`make up`)
   - **Output**: Verify: (a) `docker compose ps` shows langfuse-web, langfuse-worker, clickhouse healthy; (b) Langfuse UI at localhost:3002; (c) `langfuse` database exists in postgres; (d) ClickHouse ping responds; (e) admin-frontend healthcheck still passes
   - **Test**: Manual verification + document in acceptance criteria check

