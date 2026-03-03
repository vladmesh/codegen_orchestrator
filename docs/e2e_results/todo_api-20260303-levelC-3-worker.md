# Audit Report

## Overview
Audit of the service-template framework during implementation of a TODO API (CRUD REST endpoints).

## What Worked Well

### Spec-First Code Generation
- `make generate-from-spec` correctly generated Pydantic schemas (Todo, TodoCreate, TodoUpdate, TodoRead) from `shared/spec/models.yaml`
- Protocol generation for `TodosControllerProtocol` was accurate and matched the domain spec
- Controller stub generation saved time — boilerplate was auto-created in `services/backend/src/controllers/todos.py`
- `make validate-specs` caught spec issues before generation

### Linting and Compliance
- `make lint` runs a comprehensive suite: ruff, xenon complexity, spec validation, spec compliance, controller-protocol sync
- `make lint-controllers` correctly verified that our controller implements all protocol methods
- `make format` auto-fixed formatting issues cleanly

### Test Infrastructure
- `conftest.py` test fixtures (SQLite-backed, transactional isolation) worked out of the box for the new Todo model
- No changes needed to test infrastructure when adding a new domain

### Migration Generation
- `make makemigrations name="create_todos"` auto-detected the new `todos` table from ORM model changes
- Generated migration was correct and applied cleanly

## Issues and Observations

### 1. POSTGRES_HOST Mismatch for Native CLI
- **File**: `/workspace/.env`
- **Issue**: `.env` has `POSTGRES_HOST=project-db` which is the Docker service hostname. When running `make migrate` or `make makemigrations` natively (outside Docker), you need to override with `POSTGRES_HOST=db` (or whatever the actual hostname is on the orchestrator network).
- **Workaround**: Used `POSTGRES_HOST=db make migrate` each time.
- **Suggestion**: Document this in AGENTS.md, or provide a `make migrate-local` target that auto-sets the host, or have the `orchestrator dev-env start-infra` command print connection hints.

### 2. Documentation is Mostly in Russian
- **Files**: `AGENTS.md`, `services/backend/AGENTS.md`
- **Observation**: All framework documentation is in Russian. This is fine for Russian-speaking teams but may limit accessibility.
- **Suggestion**: Consider providing English translations or at least English section headers for key instructions.

### 3. Generated Controller is Not Read-Only
- **File**: `services/backend/src/controllers/todos.py`
- **Observation**: The framework generates this file with `NotImplementedError` stubs, but it's NOT listed as read-only (unlike `src/generated/` files). This is correct behavior — just noting that the generation is smart enough to only generate if the file doesn't exist, and doesn't overwrite manual implementations.

### 4. No REDIS_URL in .env
- **File**: `/workspace/.env`
- **Observation**: The `.env` file doesn't include `REDIS_URL` even though the framework's event system requires it. Tests work because `conftest.py` sets `os.environ.setdefault("REDIS_URL", "redis://localhost:6379")`.
- **Suggestion**: Add `REDIS_URL` to the scaffolded `.env` file if events are enabled.

### 5. ORMBase Adds `updated_at` But Todo Spec Doesn't Have It
- **Observation**: The `ORMBase` class in `core/db.py` automatically adds `created_at` and `updated_at` columns. The Todo spec only defines `created_at` as a field. The `updated_at` column still exists in the database (via ORMBase) but is not exposed in the API schema. This is acceptable behavior but could be confusing.
- **Suggestion**: Consider documenting that ORMBase always adds both timestamp columns regardless of what's in the spec.

### 6. Spec `default: ""` Generates `str | None = ""`
- **File**: `shared/shared/generated/schemas.py`
- **Observation**: Setting `default: ""` for the `description` field in models.yaml generates `description: str | None = ""` in the Pydantic schema. The `| None` part means the field accepts `None` as a value, which may not be intended when the default is an empty string.
- **Suggestion**: Consider whether `default` should make the field `Optional` or just provide a default value while keeping the type strict.

## Summary

The framework is well-designed and the spec-first approach significantly reduces boilerplate. The main areas for improvement are around documentation (English support, migration host hints) and minor schema generation edge cases. Overall, the development experience was smooth — the entire TODO API implementation (spec, codegen, ORM, repository, controller, router, migration, tests) was completed without any blocking issues.

