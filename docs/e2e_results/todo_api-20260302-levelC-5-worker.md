# Audit Report — todo_api

## Summary

Built a complete TODO CRUD API (GET/POST/PATCH/DELETE /todos) following the spec-first framework conventions. All 20 tests pass, all linters clean.

## Issues Encountered

### 1. `orchestrator dev-env start-infra db` fails

**Error:**
```
stat /tmp/codegen/workspaces/.../workspace/docker-compose.yml: no such file or directory
```

**Expected:** The orchestrator should find compose files in `infra/compose.base.yml` or `infra/compose.dev.yml`.
**What happened:** The orchestrator looks for a `docker-compose.yml` at the repo root, which doesn't exist. The project uses split compose files under `infra/`.
**Impact:** Cannot run `make makemigrations` (which requires a live PostgreSQL) or any infrastructure-dependent commands. Had to write the migration file manually instead of using Alembic autogenerate.
**Suggestion:** Either the orchestrator should be configured to find compose files in `infra/`, or a `docker-compose.yml` symlink/wrapper should exist at the repo root.

### 2. Manual migration writing required

**Context:** Because the database could not be started (see issue #1), I could not use `make makemigrations name="create_todos"` to autogenerate the migration.
**Workaround:** Wrote `services/backend/migrations/versions/0002_create_todos.py` manually, following the pattern of the existing `118f8b3895d8_create_user.py` migration.
**Risk:** Manual migrations may miss subtle differences that autogenerate would catch (e.g., index naming conventions, constraint names).
**Suggestion:** Consider adding an offline migration generation mode or SQLite-compatible autogenerate for environments without PostgreSQL access.

### 3. Alembic env.py uses sync driver for autogenerate

**Context:** Alembic's `env.py` uses `settings.sync_database_url` which defaults to `postgresql+psycopg` driver. When trying to override with `DATABASE_URL=sqlite:///...` for offline generation, it still requires psycopg to be installed and doesn't handle SQLite gracefully.
**Suggestion:** The migration env.py could detect when a SQLite URL is provided and adjust the migration context accordingly. This would allow running autogenerate in test/CI environments without PostgreSQL.

### 4. AGENTS.md is in Russian

**Observation:** All documentation files (`AGENTS.md`, `services/backend/AGENTS.md`, `CONTRIBUTING.md`) are written in Russian. While this works, it may be a barrier for non-Russian-speaking contributors or AI agents that perform better with English documentation.
**Suggestion:** Consider having English versions or bilingual docs, especially for the AGENTS.md files that AI agents consume.

### 5. Framework code generation works well

**Positive:** The `make generate-from-spec` workflow worked perfectly:
- Added Todo model to `shared/spec/models.yaml`
- Created `services/backend/spec/todos.yaml` for domain operations
- Code generation produced correct Pydantic schemas, Protocol classes, and controller stubs
- The `lint-controllers` check correctly validates controller implementations match protocols
- Spec validation catches errors before generation

### 6. Test infrastructure is solid

**Positive:** The test setup in `conftest.py` is well-designed:
- Uses SQLite for fast unit tests without infrastructure dependencies
- Savepoint-based transaction isolation between tests
- Mock broker for Redis events
- Proper cleanup of test database

### 7. `compose.base.yml` has `ports:` directive

**File:** `infra/compose.base.yml`, line 20: `ports: - "8000:8000"`
**Issue:** The `CLAUDE.md` instructions explicitly say "Never add `ports:` directives to compose files. Services communicate by hostname on the internal network. Publishing ports causes conflicts between parallel workers." However, the base compose file itself has a ports directive for the backend service.
**Suggestion:** Move the `ports:` directive to `compose.dev.yml` only, or remove it entirely and rely on hostname-based communication.

## Files Created/Modified

### New files:
- `services/backend/spec/todos.yaml` — domain spec for todos operations
- `services/backend/src/app/models/todo.py` — SQLAlchemy ORM model
- `services/backend/src/app/repositories/todo.py` — data access layer
- `services/backend/src/app/api/routers/todos.py` — FastAPI router
- `services/backend/migrations/versions/0002_create_todos.py` — database migration
- `services/backend/tests/unit/test_todos.py` — 13 test cases

### Modified files:
- `shared/spec/models.yaml` — added Todo model definition
- `services/backend/src/app/models/__init__.py` — added Todo export
- `services/backend/src/app/repositories/__init__.py` — added TodoRepository export
- `services/backend/src/app/schemas/__init__.py` — added Todo schema exports
- `services/backend/src/app/api/router.py` — registered todos router

### Auto-generated (by framework):
- `shared/shared/generated/schemas.py` — Pydantic models (Todo, TodoCreate, TodoUpdate, TodoRead)
- `services/backend/src/generated/protocols.py` — TodosControllerProtocol
- `services/backend/src/controllers/todos.py` — controller stub (then implemented)

