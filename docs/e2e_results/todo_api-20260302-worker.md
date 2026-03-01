# Audit Report

## Overview
Audit performed while implementing the `todo_api` project — a REST API for TODO items with GET/POST/PATCH/DELETE /todos endpoints.

## Framework & Tooling Observations

### What Worked Well

1. **Spec-first workflow is smooth.** Editing `shared/spec/models.yaml` and `services/backend/spec/todos.yaml`, then running `make generate-from-spec` produced correct Pydantic schemas, Protocol classes, and a controller stub. The whole flow took seconds.

2. **Generated controller stubs are helpful.** The `TodosController` stub was generated with correct method signatures matching the protocol. Only the business logic needed to be filled in.

3. **Linting and compliance checks are comprehensive.** `make lint` runs ruff, xenon complexity, spec validation, spec compliance, and controller sync checks. All caught issues early.

4. **Test infrastructure is solid.** The SQLite-based test setup with savepoint isolation (`conftest.py`) makes unit tests fast and reliable without requiring a live database.

5. **`EXEC_MODE=native` works as documented.** All make targets (`lint`, `format`, `tests`, `generate-from-spec`, `validate-specs`) run natively without Docker as expected.

### Issues & Problems

1. **`make makemigrations` requires Docker but instructions say to use it.**
   - **Expected:** A native way to create migrations (consistent with `EXEC_MODE=native` for other targets).
   - **Actual:** `make makemigrations` runs `docker compose ... run --rm backend alembic ...`, which fails without Docker.
   - **Workaround:** Created the migration file manually following the pattern in existing migration `118f8b3895d8_create_user.py`.
   - **Suggestion:** Add an `EXEC_MODE=native` path for `make makemigrations` that runs Alembic directly.

2. **Generated `TodoUpdate` schema has potentially confusing defaults.**
   - For fields with `default` values in the spec (e.g., `description: default ""`, `is_completed: default false`), the generated `TodoUpdate` schema keeps those defaults instead of using `None`.
   - **Generated:** `description: str | None = ""` and `is_completed: bool | None = False`
   - **Expected for PATCH:** `description: str | None = None` and `is_completed: bool | None = None`
   - **Impact:** The business logic works correctly because we use `model_dump(exclude_unset=True)`, but the OpenAPI documentation shows misleading defaults for the update schema.
   - **Suggestion:** For Update variants, override defaults to `None` for all optional fields regardless of the base model's default.

3. **Protocol `.py` has inconsistent indentation.**
   - The generated `protocols.py` has irregular spacing in method signatures (extra leading spaces before params/payload).
   - **Example:** Line 33 `        payload: CommandReceivedCreate,` has extra indentation relative to `self` and `session`.
   - **Impact:** Cosmetic only — ruff doesn't flag it, but it looks messy.
   - **File:** `services/backend/src/generated/protocols.py`
   - **Suggestion:** Fix the Jinja2 template `protocols.py.j2` indentation handling.

4. **No router auto-generation.**
   - Protocols and controller stubs are auto-generated, but REST routers must be written manually.
   - **Impact:** Boilerplate duplication — the router file closely mirrors the spec and protocol. Each endpoint follows an identical pattern (create FastAPI route, inject dependencies, delegate to controller).
   - **Suggestion:** Consider generating routers from the spec as well (like protocols), or at least generating stubs.

5. **AGENTS.md and documentation are in Russian.**
   - `AGENTS.md`, `services/backend/AGENTS.md`, `CONTRIBUTING.md`, `ARCHITECTURE.md` contain Russian text.
   - **Impact:** Non-Russian-speaking developers may struggle to follow conventions.
   - **Suggestion:** Provide English translations or maintain bilingual docs.

### Minor Observations

- The `ORMBase` adds both `created_at` and `updated_at` columns. For models that only expose `created_at` in the API (like Todo), the `updated_at` column exists in the DB but is unused in the schema. This is fine but slightly wasteful.
- The `conftest.py` approach of setting all environment variables via `os.environ.setdefault()` works but could be cleaner with a `.env.test` file.
- The `_to_schema()` pattern for handling SQLite's naive datetimes (adding UTC tzinfo) is duplicated per controller. A shared utility would reduce duplication.
- The `extra="forbid"` config on all generated schemas is a good safety measure for strict API contracts.

## Files Created/Modified

### New Files
- `shared/spec/models.yaml` — Added `Todo` model with variants
- `services/backend/spec/todos.yaml` — Domain operations for todos
- `services/backend/src/app/models/todo.py` — Todo ORM model
- `services/backend/src/app/repositories/todo.py` — TodoRepository
- `services/backend/src/controllers/todos.py` — TodosController implementation
- `services/backend/src/app/api/routers/todos.py` — REST router for /todos
- `services/backend/migrations/versions/0003_create_todo.py` — Migration for todos table
- `services/backend/tests/unit/test_todos.py` — 11 unit tests for all CRUD operations

### Modified Files
- `services/backend/src/app/models/__init__.py` — Added Todo import
- `services/backend/src/app/repositories/__init__.py` — Added TodoRepository import
- `services/backend/src/app/schemas/__init__.py` — Added Todo schema imports
- `services/backend/src/app/api/router.py` — Registered todos router

### Auto-Generated (by framework)
- `shared/shared/generated/schemas.py` — Todo, TodoCreate, TodoUpdate, TodoRead
- `services/backend/src/generated/protocols.py` — TodosControllerProtocol

