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

## Problems Found

### Problem 1: `.env` has wrong `POSTGRES_HOST` for orchestrator usage
- **Severity**: minor
- **Type**: template
- **Backlog**: — (root cause was stale worker-manager image, see weather_bot-20260304-levelC.md Problem 1)
- **Description**: `POSTGRES_HOST=project-db` but orchestrator uses `db`. With proper worker network isolation (codegen_worker), this is moot — workers can't reach orchestrator DB.

### Problem 2: ORMBase forces `updated_at` on all models
- **Severity**: minor
- **Type**: template
- **Backlog**: service-template backlog
- **Description**: `ORMBase` bundles both `created_at` and `updated_at`. Models needing only `created_at` must use `Base` directly. Fix: add `CreatedAtBase` mixin.

### Problem 3: No router code generation from specs
- **Severity**: minor
- **Type**: template
- **Backlog**: service-template backlog
- **Description**: Domain spec has all info to generate routers but they must be written manually. Most boilerplate-heavy part.

### Problem 4: No `__init__.py` re-export updates after generation
- **Severity**: minor
- **Type**: template
- **Backlog**: service-template backlog
- **Description**: `schemas/__init__.py`, `models/__init__.py`, `repositories/__init__.py` need manual updates after adding new domains.

### Problem 5: AGENTS.md in Russian
- **Severity**: info
- **Type**: template
- **Backlog**: — (cosmetic, by design)
- **Description**: Documentation is mostly in Russian. Code examples are clear but explanatory text requires translation.

### Problem 6: `description` field default not propagated to Create variant
- **Severity**: minor
- **Type**: template
- **Backlog**: — (edge case, workaround: handle None→"" in repository)
- **Description**: Spec `default: ""` with `optional: [description]` generates `str | None = None` instead of `str = ""`. Repository must handle conversion.

