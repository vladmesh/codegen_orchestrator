# Audit Report

## Overview

Audit performed while implementing a Todo CRUD API (`GET/POST/PATCH/DELETE /todos`) on the service-template framework.

## What Worked Well

1. **Spec-first code generation**: The `make generate-from-spec` workflow worked seamlessly. Defining the Todo model in `shared/spec/models.yaml` and domain operations in `services/backend/spec/todos.yaml` correctly generated Pydantic schemas (`shared/shared/generated/schemas.py`), controller protocols (`services/backend/src/generated/protocols.py`), and a controller stub (`services/backend/src/controllers/todos.py`).

2. **Clear patterns from existing code**: The User domain provided a complete, well-structured reference implementation (ORM model, repository, controller, router, tests). Following the pattern was straightforward.

3. **Spec validation**: `make validate-specs` caught issues early before generation. The linting pipeline (`ruff check`, `xenon`, spec compliance, controller sync) all passed cleanly.

4. **Test infrastructure**: The test fixtures (`conftest.py`) with SQLite-backed async sessions, per-test transaction rollback, and mocked event broker worked well. Tests ran quickly (18 tests in ~1.3s).

5. **Controller sync linting**: `make lint-controllers` automatically verified that the implemented controllers match their generated protocols — a nice safety net.

## Problems Encountered

### 1. Infrastructure orchestrator failure (Critical)

**What happened**: `orchestrator dev-env start-infra db` returned a `500 Internal Server Error`:
```
Error: Server error '500 Internal Server Error' for url
'http://worker-manager:8000/api/worker/dev-todo-api-127903ce/infra/compose'
```

**Impact**: Could not use `make makemigrations` to auto-generate Alembic migrations (requires a running PostgreSQL). Had to write the migration file manually.

**Expected**: The orchestrator should either start the database or provide a clear error message explaining why it can't.

**Workaround**: Wrote `services/backend/migrations/versions/0002_create_todos.py` by hand, following the pattern of the existing `118f8b3895d8_create_user.py` migration.

### 2. Generated schemas use `shared.generated.schemas` import path

**Observation**: The generated code uses `from shared.generated.schemas import ...` but the actual package path on disk is `shared/shared/generated/schemas.py`. This works because `shared/` is installed as a package, but the double `shared/shared/` directory structure is confusing.

**Suggestion**: Consider flattening the shared package so the directory structure matches the import path more intuitively (e.g., `shared/generated/schemas.py` mapping to `shared.generated.schemas`).

### 3. No `list` operation example in scaffolded User domain

**Observation**: The scaffolded User domain doesn't have a `list_users` operation, so when implementing `list_todos` there was no direct example of a list endpoint in the existing codebase. The `AGENTS.md` documentation did include a list example, which was helpful.

**Suggestion**: Consider including a list operation in the scaffolded User domain spec to provide a complete CRUD reference.

### 4. ORMBase includes `updated_at` but Todo spec doesn't need it

**Observation**: `ORMBase` in `services/backend/src/core/db.py` automatically adds both `created_at` and `updated_at` columns. The Todo spec only defines `created_at` as a field, but the ORM model inherits `updated_at` from `ORMBase`. This is fine for the database, but means the `TodoRead` schema doesn't expose `updated_at` even though it exists in the database. This could be confusing if someone queries the DB directly.

**Suggestion**: Document that `ORMBase` adds `created_at` and `updated_at` automatically, so spec authors know these columns exist regardless of their spec definition.

### 5. Manual wiring still required

**Observation**: After code generation, several manual steps were needed:
- Create the ORM model (`src/app/models/todo.py`)
- Create the repository (`src/app/repositories/todo.py`)
- Create the router (`src/app/api/routers/todos.py`)
- Update `__init__.py` files (models, repositories, schemas)
- Wire the router in `src/app/api/router.py`
- Implement controller business logic

**Suggestion**: The framework could optionally scaffold the router and ORM model boilerplate (not just the controller stub), reducing the manual steps needed for a new domain. Even generating the `__init__.py` updates would save time.

## Suggestions for Improvement

1. **Scaffold routers alongside controllers**: When `make generate-from-spec` creates a new controller stub, also scaffold a corresponding router file with the correct endpoints, imports, and dependency injection.

2. **Scaffold ORM models**: Generate a basic SQLAlchemy model from the spec's field definitions, similar to how Pydantic schemas are generated.

3. **Auto-update `__init__.py` re-exports**: When a new domain is added, auto-update the barrel files (`models/__init__.py`, `repositories/__init__.py`, `schemas/__init__.py`).

4. **Better error handling from orchestrator**: Surface clear error messages when infrastructure commands fail, especially for common cases like database not being available.

5. **Add a `make new-domain` command**: A single command that creates the spec file, generates code, scaffolds the ORM model/repository/router, and creates a test file template would significantly speed up development.

