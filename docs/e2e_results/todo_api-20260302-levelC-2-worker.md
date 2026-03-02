# Audit Report

## Overview

Audit performed while implementing the TODO API (GET/POST/PATCH/DELETE /todos) on the `service-template` framework.

## What Worked Well

1. **Spec-first code generation** — The `make generate-from-spec` workflow is excellent. Defining models in `shared/spec/models.yaml` and domain operations in `services/backend/spec/todos.yaml` cleanly generated Pydantic schemas (`shared/shared/generated/schemas.py`), controller protocols (`services/backend/src/generated/protocols.py`), and even a controller stub (`services/backend/src/controllers/todos.py`). This saved significant boilerplate.

2. **Clear project structure** — The separation of concerns (models, repositories, controllers, routers, generated code) is well-organized and easy to follow. The existing User domain served as a perfect pattern to copy.

3. **Linter/spec compliance tooling** — `make lint`, `make validate-specs`, `make lint-controllers` all work correctly and catch real issues. The `lint-controllers` check that verifies controllers match generated protocols is particularly valuable.

4. **Test infrastructure** — The SQLite-based test setup in `conftest.py` with per-test transactional rollback works smoothly. Adding a new domain's tests required zero changes to the test infrastructure.

5. **Controller stub generation** — The framework automatically generated a controller stub with all methods from the protocol, each raising `NotImplementedError`. This is a great starting point.

## Issues Encountered

### 1. Generated schemas default values for optional Update fields
**File:** `shared/shared/generated/schemas.py`
**Issue:** The generated `TodoUpdate` schema has `description: str | None = ""` and `is_completed: bool | None = False` instead of defaulting to `None`. While this works with Pydantic's `exclude_unset=True` for PATCH semantics, it's semantically confusing — the type says `str | None` but the default is `""`, not `None`. This could trip up developers who check `if field is None` instead of using `exclude_unset=True`.
**Expected:** Update variant fields should default to `None` to clearly indicate "not provided".

### 2. Generated protocols formatting
**File:** `services/backend/src/generated/protocols.py`
**Issue:** The generated protocol file has inconsistent indentation — some parameters have extra leading whitespace (e.g., `                        payload: TodoCreate,` with 24 spaces). While functionally correct, it doesn't match ruff formatting standards. Running `ruff format` on generated files would produce different output.
**Suggestion:** Run ruff format on generated output, or use consistent 8-space indentation for continuation lines.

### 3. Router is not auto-generated
**Issue:** The framework generates schemas, protocols, and controller stubs, but routers must be written manually. The AGENTS.md documents this, but given that the spec already contains all the information needed (HTTP method, path, status code, input/output types), the router could be auto-generated too.
**Suggestion:** Add router generation to `make generate-from-spec`. The router boilerplate is highly predictable and follows a strict pattern.

### 4. `__init__.py` files not updated by codegen
**Issue:** After adding a new domain, I had to manually update:
  - `services/backend/src/app/models/__init__.py`
  - `services/backend/src/app/repositories/__init__.py`
  - `services/backend/src/app/schemas/__init__.py`
  - `services/backend/src/app/api/router.py`
**Suggestion:** Either auto-update these files during codegen or document clearly that they need manual updates for each new domain.

### 5. ORMBase includes `updated_at` but Todo spec only has `created_at`
**File:** `services/backend/src/core/db.py`
**Issue:** `ORMBase` includes both `created_at` and `updated_at` columns. The Todo spec only defines `created_at` as a readonly field, but the ORM model inherits `updated_at` from `ORMBase`. This creates a mismatch between the spec and the actual database schema. The generated `TodoRead` schema does not include `updated_at`, but it exists in the DB.
**Workaround:** This works because `model_validate(from_attributes=True)` with `extra="forbid"` simply ignores extra attributes from the ORM object. But it's a hidden discrepancy.
**Suggestion:** Consider making `ORMBase` configurable or providing a `TimestampMixin` that can be opted into.

### 6. Trailing whitespace in scaffolded file
**File:** `services/backend/src/app/lifespan.py`
**Issue:** The scaffolded `lifespan.py` had a trailing blank line that `ruff format` removed. Minor, but suggests the template itself has a formatting inconsistency.

## Suggestions for Improvement

1. **Auto-generate routers** — The router pattern is completely predictable from the spec. This would eliminate the most tedious part of adding a new domain.

2. **Scaffolding command for new domains** — A `make new-domain name=todos` command that creates the spec file, ORM model skeleton, repository skeleton, and wires up all the `__init__.py` files would significantly speed up development.

3. **Document the full workflow** — The AGENTS.md for backend is good, but a step-by-step "Adding a new domain" guide would help. Currently you need to piece together info from multiple files.

4. **Test template generation** — Similar to controller stubs, generate test file stubs for new domains with basic CRUD test patterns.

5. **Migration tooling for dev** — The `make makemigrations` command requires Docker Compose. For native development (`EXEC_MODE=native`), there's no easy way to auto-generate migrations. A native migration generation path would be useful.

