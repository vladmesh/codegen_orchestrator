# Audit Report

## Overview
Built a TODO CRUD API (`GET/POST/PATCH/DELETE /todos`) following the spec-first framework workflow. This report documents observations, issues, and suggestions encountered during development.

## What Worked Well

1. **Spec-first workflow**: Editing `models.yaml` + domain spec → `make generate-from-spec` → protocols + controller stubs auto-generated. Very smooth and productive.
2. **Generated controller stubs**: The framework generated a working `TodosController` skeleton with correct method signatures matching the protocol. Significantly reduced boilerplate.
3. **Validation and linting pipeline**: `make validate-specs`, `make lint`, `make lint-controllers` all work correctly and catch real issues (import ordering, spec compliance, controller sync).
4. **Test infrastructure**: The `conftest.py` setup with SQLite for unit tests, transactional fixtures, and mock broker is well-designed and fast (~1s for 19 tests).
5. **`list[Model]` return type support**: The framework correctly handled `output: list[TodoRead]` in the spec, generating the right protocol signature (`-> list[TodoRead]`).

## Issues Encountered

### 1. Routers are NOT auto-generated (manual work required)
- **What happened**: After running `make generate-from-spec`, protocols and controller stubs were generated, but routers were not. I had to manually create `services/backend/src/app/api/routers/todos.py` and wire it into `services/backend/src/app/api/router.py`.
- **Impact**: This is the most significant manual step. Each new domain requires creating a router file (~80 lines of boilerplate) and updating `router.py`.
- **Suggestion**: Auto-generate routers from the domain spec, since all the information (prefix, tags, methods, paths, status codes, parameter types) is already in the YAML. The router pattern is 100% mechanical.

### 2. ORM models and repositories are NOT generated
- **What happened**: Had to manually create `services/backend/src/app/models/todo.py` and `services/backend/src/app/repositories/todo.py`, then update both `__init__.py` files.
- **Impact**: Moderate. The models and repos follow predictable patterns derived from the spec.
- **Suggestion**: Consider generating at least the ORM model skeleton from `models.yaml`, since field types, defaults, and constraints are already defined there.

### 3. Migration requires Docker but `EXEC_MODE=native` doesn't support it
- **What happened**: `make makemigrations` runs via `docker compose`, but the workspace is set up for `EXEC_MODE=native`. Had to write the migration manually.
- **Expected**: A native-mode alternative for creating migrations (e.g., running alembic directly against a temporary database).
- **Suggestion**: Add a `make makemigrations EXEC_MODE=native` target that can use a local SQLite or connect to infrastructure started via `orchestrator dev-env`.

### 4. Generated schema defaults may cause confusion with PATCH semantics
- **File**: `shared/shared/generated/schemas.py`
- **What happened**: `TodoUpdate` generates `description: str | None = ""` and `is_completed: bool | None = False`. While `model_dump(exclude_unset=True)` handles this correctly at runtime, the schema defaults (`""` and `False`) differ from the `None` default that `UserUpdate` uses for optional fields.
- **Impact**: Low (works correctly), but could confuse developers reviewing the schema who might not realize `exclude_unset` is used.
- **Suggestion**: For `Update` variants, consider generating all optional fields with `default=None` instead of carrying over the create-time defaults.

### 5. Documentation is partially in Russian
- **Files**: `AGENTS.md`, `services/backend/AGENTS.md`, `CONTRIBUTING.md`
- **What happened**: All documentation in these files is in Russian. As an English-speaking developer this requires translation.
- **Suggestion**: Provide English translations or maintain bilingual docs.

### 6. `shared/generated/` path inconsistency
- **What happened**: The import path is `shared.generated.schemas` but the actual file lives at `shared/shared/generated/schemas.py`. This double `shared/shared/` nesting is unusual and initially confusing.
- **Suggestion**: Either flatten to `shared/generated/` or document the package structure clearly.

## Minor Observations

- **`make format`** correctly fixed the import ordering issue in `services/backend/src/app/repositories/todo.py` (ruff I001). The ruff integration works well.
- **Controller sync linter** (`make lint-controllers`) correctly validates that controller implementations match protocol signatures. Very useful for spec-first.
- **Test conftest** uses `os.environ.setdefault` to set up test env vars. This works but means running tests without the conftest loading first will fail with `RuntimeError` from `Settings._validate_required_env_vars`. This is by design and appropriate.

## Suggestions for Improvement

1. **Router auto-generation**: This is the #1 improvement. All info exists in the spec to generate routers automatically.
2. **Scaffolding command**: A `make scaffold-domain name=todos` command that generates the ORM model, repository, router, `__init__.py` updates, and test file would dramatically speed up development.
3. **Native migration support**: Allow creating migrations without Docker.
4. **Update variant defaults**: Use `None` defaults for all `Update` variant fields to make PATCH semantics clearer.

