# Audit Report

## Overview
Audit performed while implementing the Todo API (GET/POST/PATCH/DELETE /todos) using the service-template framework.

## What Worked Well

1. **Spec-first code generation**: Defining models in `shared/spec/models.yaml` and domain operations in `services/backend/spec/todos.yaml`, then running `make generate-from-spec` produced correct Pydantic schemas, Protocol classes, and controller stubs. This workflow is smooth and reduces boilerplate.

2. **Validation pipeline**: `make validate-specs` correctly validated YAML specs before generation. `make lint-controllers` correctly verified controller-protocol synchronization.

3. **Controller stub generation**: The generated `TodosController` stub in `services/backend/src/controllers/todos.py` had the correct method signatures matching the protocol — just needed business logic implementation.

4. **Test infrastructure**: The `conftest.py` with SQLite-backed transactional test sessions works well. Tests run fast (22 tests in ~1.5s) with proper isolation between tests.

5. **Alembic integration**: `make makemigrations name="create_todos"` correctly auto-detected the new `todos` table from the ORM model. `make migrate` applied cleanly.

6. **Linting suite**: `make lint` runs ruff, xenon complexity checks, spec validation, spec compliance, and controller sync checks — comprehensive and catches issues early.

## Issues and Problems

### 1. `.env` file has wrong `POSTGRES_HOST` for orchestrator usage
- **File**: `/workspace/.env`
- **Issue**: `POSTGRES_HOST=project-db` but when using `orchestrator dev-env start-infra db`, the database hostname is `db` (the service name in compose).
- **Impact**: Had to override `POSTGRES_HOST=db` when running `make migrate` and `make makemigrations`. Without this override, migration commands would fail to connect.
- **Suggestion**: Either document the correct hostname when using orchestrator, or make the `.env` default match the orchestrator's service naming. Alternatively, the TASK.md/AGENTS.md could mention this discrepancy.

### 2. `TodoRead` schema has `extra="forbid"` — incompatible with `updated_at` from `ORMBase`
- **File**: `shared/shared/generated/schemas.py`
- **Issue**: The generated `TodoRead` schema uses `extra="forbid"` from Pydantic's ConfigDict. The `ORMBase` class automatically adds `created_at` and `updated_at` columns, but if the spec only includes `created_at`, the `model_validate(orm_obj, from_attributes=True)` call would fail if `updated_at` were not explicitly handled. In practice this works because `from_attributes=True` only picks up fields defined on the model, but it's a potential source of confusion.
- **Suggestion**: Consider documenting this behavior or making `extra="forbid"` configurable per model variant.

### 3. Routers are manual but could be generated
- **Issue**: The domain spec (`spec/todos.yaml`) has all the information needed to generate routers (endpoints, methods, status codes, input/output types, path params). Currently, routers must be written manually in `src/app/api/routers/` and registered manually in `src/app/api/router.py`.
- **Suggestion**: Consider generating routers from the domain spec, similar to how protocols and controllers are generated. This would reduce boilerplate and prevent drift between spec and router definitions.

### 4. No generated `__init__.py` updates for models/repositories
- **Issue**: When adding a new domain (e.g., todos), the developer must manually update `src/app/models/__init__.py`, `src/app/repositories/__init__.py`, and `src/app/schemas/__init__.py` to export the new classes. The code generator doesn't touch these files.
- **Suggestion**: Either generate these `__init__.py` files or document explicitly that they need manual updates after adding a new domain.

### 5. AGENTS.md is in Russian
- **File**: `/workspace/AGENTS.md`, `/workspace/services/backend/AGENTS.md`
- **Issue**: Documentation is mostly in Russian, which may be a barrier for non-Russian-speaking developers. The code examples are clear, but the explanatory text requires translation.
- **Suggestion**: Consider providing English versions or bilingual documentation for wider accessibility.

### 6. Minor: `description` field default handling
- **Issue**: In `models.yaml`, the Todo model has `description` with `default: ""` and `optional: [description]` in the Create variant. The generated `TodoCreate` schema makes `description` optional as `str | None = None` (not `str = ""`). This means the repository must handle `None` → `""` conversion explicitly.
- **Expected**: The generated `TodoCreate` could default `description` to `""` instead of `None` when the model spec has `default: ""`.
- **Workaround**: Handle `None` to `""` conversion in the repository's `create` method.

## Suggestions for Improvement

1. **Router auto-generation**: Generate FastAPI routers from domain spec files to eliminate manual router boilerplate.
2. **Init file management**: Auto-update or generate `__init__.py` files for models, repositories, and schemas when new domains are added.
3. **Orchestrator hostname documentation**: Add a note to AGENTS.md or CONTRIBUTING.md about the correct database hostname (`db`) when using `orchestrator dev-env start-infra`.
4. **Template for tests**: Consider generating test stubs alongside controller stubs for new domains.
5. **Default value propagation**: Ensure model field defaults propagate correctly to Create variants in generated schemas.

