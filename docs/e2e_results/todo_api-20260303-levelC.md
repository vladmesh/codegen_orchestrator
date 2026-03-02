# E2E Report: todo_api — Deploy failed: BACKEND_PORT set to random secret instead of port number

> **Date**: 2026-03-03
> **Project**: todo_api (project_id: `da677462-fdf6-4510-82a6-cdcbc27c4f72`)
> **Task**: eng-a4b01648d3a7
> **Test level**: C
> **Status**: Failed
> **Worker audit**: [todo_api-20260303-levelC-worker.md](./todo_api-20260303-levelC-worker.md)

---

## Timeline

| Time (UTC) | Event |
|------------|-------|
| 23:05:07 | Project created, engineering task published to queue |
| 23:05:14 | Worker container `worker-dev-todo-api-db3b10c5` created |
| 23:05:14 | Scaffold phase started (copier template: `gh:vladmesh/service-template`) |
| 23:05:28 | Scaffold phase completed and verified (14s) |
| 23:05:29 | Claude agent started implementation |
| 23:06:13 | CI Run #22599905480 (scaffold): **passed** |
| 23:14:02 | Implementation commit: `feat: implement Todo CRUD API with GET/POST/PATCH/DELETE /todos` |
| 23:14:08 | CI Run #22600158693 (implementation): **passed** |
| 23:14:21 | Worker wrapper captured session and commit SHA (`6c60f4e9`) |
| ~23:16:42 | Engineering task completed, deploy secrets configured (9 secrets) |
| 23:16:44 | Deploy workflow dispatched |
| 23:17:48 | Deploy Run #22600236664: **failed** — `invalid hostPort: QkSBLev68L23BgZz_C9eQZ4WsjT46gyD4YSaUXXDpj0` |
| 23:17:49 | Rerun of failed jobs triggered |
| 23:18:54 | Rerun also **failed** — same error |
| 23:18:54 | Deploy task marked as failed |

**Engineering duration**: ~11 min (scaffold 14s, implementation ~9 min, CI ~2 min)
**Deploy duration**: ~2 min (both attempts failed)

## Engineering Results

- 3 commits, 2 CI runs — all CI passed first try
- No CI fix cycles needed
- Worker produced AUDIT_REPORT.md

## Problems Found

### Problem 1: BACKEND_PORT set to random secret instead of port number (DEPLOY BLOCKER)
- **Type**: orchestrator
- **Severity**: critical
- **Description**: The deployed `.env` file contains `BACKEND_PORT=QkSBLev68L23BgZz_C9eQZ4WsjT46gyD4YSaUXXDpj0` — a random `token_urlsafe(32)` string instead of the allocated port number `8000`. This causes `docker compose pull` to fail with `invalid hostPort` when parsing port mapping `${BACKEND_PORT}:8000` (compose validates all directives including `ports:` even on `pull`).
- **Trigger**: service-template commit `1f406a4` (2026-03-02 17:37 UTC) changed `compose.base.yml` from hardcoded `ports: "8000:8000"` to dynamic `ports: "${BACKEND_PORT:-8000}:8000"` and added `BACKEND_PORT=8000` to `.env.example`. This was a correct fix for the port allocation mismatch reported in the previous E2E run (`todo_api-20260302-levelC-5`, Problem 1). However, the orchestrator's DevOps subgraph was not updated to handle the new variable.
- **Root cause**: Two-layer bug in the DevOps subgraph env resolution:
  1. **LLM classification**: `env_analyzer.py` sees `BACKEND_PORT` in `.env.example` (new since the template change), sends it to LLM. The LLM prompt says "Internal ports and hosts" → INFRA. LLM classifies `BACKEND_PORT` as "infra".
  2. **Missing port handler**: `_generate_infra_secret()` in `nodes.py` handles `POSTGRES_PORT` (hardcoded "5432") but has no handler for service-specific port variables like `BACKEND_PORT`. Fallback: `secrets.token_urlsafe(32)`.
  3. The port allocator correctly allocates port 8000 in `allocated_resources`, but the secret resolver never reads it for `BACKEND_PORT`.
- **Why it worked before**: Previous scaffolds had `ports: "8000:8000"` hardcoded — `BACKEND_PORT` didn't exist in `.env.example`, so the DevOps subgraph never encountered it.
- **Files involved**:
  - `services/langgraph/src/subgraphs/devops/env_analyzer.py` — LLM prompt and classification
  - `services/langgraph/src/subgraphs/devops/nodes.py` — SecretResolverNode, `_generate_infra_secret()`, `_compute_secret()`
  - `services/langgraph/src/tools/allocator.py` — Resource allocation with port info
- **Suggested fix**: Extract a shared helper `_find_allocation(state, service_name) -> (ip, port)` in `SecretResolverNode` and use it for both `*_PORT` and `*_API_URL` variables:
  1. Add pattern in `env_analyzer.py`: `*_PORT` (except `POSTGRES_PORT`) → COMPUTED.
  2. Add `_find_allocation()` helper that looks up `allocated_resources` by `service_name`.
  3. `BACKEND_PORT` / `FRONTEND_PORT` / `TG_BOT_PORT` → `_find_allocation(state, "backend")` → `str(port)`.
  4. `BACKEND_API_URL` / `API_URL` etc. → same helper → `http://{ip}:{port}` (currently uses `first_resource` blindly — desync risk with multi-module projects).
  - Single source of truth for port resolution, no risk of `BACKEND_PORT=8001` but `BACKEND_API_URL=http://...:8000`.

### Problem 2: No `make migrate` target — worker can't apply existing migrations
- **Type**: template
- **Severity**: major
- **Description**: Worker tried `make makemigrations` after starting infra via `orchestrator dev-env start-infra db`. Got `Target database is not up to date` — scaffold creates initial migrations (0001_initial, create_user) but they need to be applied before autogenerate works. Worker then tried `alembic upgrade head` directly but env vars weren't loaded (only `make` loads `.env` via `-include .env` + `export`). Worker gave up and wrote migration manually.
- **Reproduced**: Full chain works when done correctly: `orchestrator dev-env start-infra db` → `source .env && alembic upgrade head` → `make makemigrations name='...'`. DB connectivity, DNS (`project-db`), and password auth all work fine. The blocker is purely workflow: no `make migrate` target exists.
- **Root cause**: Generated `Makefile` (from `template/Makefile.jinja`) has `makemigrations` but no `migrate` target for `alembic upgrade head`. Worker agent doesn't know to `source .env` before running alembic directly.
- **Impact**: Worker writes migrations manually instead of using autogenerate — works but risks subtle schema errors.
- **Suggested fix**: Add `migrate` target to `template/Makefile.jinja`: `PYTHONPATH=. services/backend/.venv/bin/alembic -c ... upgrade head`. Same pattern as `makemigrations`.

### Problem 3: AGENTS.md in Russian (recurring)
- **Type**: template
- **Severity**: minor
- **Description**: All generated documentation (AGENTS.md, CONTRIBUTING.md) is in Russian.
- **Suggested fix**: Generate English docs or bilingual versions.

## Positive Observations
- First-try CI pass — no fix cycles needed
- Fast implementation: ~9 min from agent start to code push
- Spec-first code generation worked well (worker's audit confirms)
- Test infrastructure (SQLite-based unit tests) is solid
- Scaffold completed in 14s
