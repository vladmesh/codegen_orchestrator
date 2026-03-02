# Audit Report

## Overview

Audit performed while implementing `todo_api` — a REST API for TODO items with CRUD operations.

## Issues Encountered

### 1. `response_list` not supported in REST spec — no documented way to declare list endpoints

**Severity:** Medium
**Location:** `services/backend/spec/todos.yaml`
**Expected:** A clear, documented way to declare list/collection endpoints (e.g., `response_list: true` in the `rest` config).
**What happened:** Used `response_list: true` in the `rest` config block and got validation error:
```
domain.response_list: Extra inputs are not permitted
```
**Workaround:** Found by reading framework source that the correct approach is to use `list[ModelName]` in the `output` field:
```yaml
list_todos:
  output: list[TodoRead]  # correct
```
**Suggestion:** Document this pattern in AGENTS.md or ARCHITECTURE.md. The spec-first workflow documentation only shows single-model output examples. A list endpoint example would save time.

### 2. Router generation is incomplete — routers not auto-generated for new domains

**Severity:** Medium
**Location:** `services/backend/src/app/api/routers/`
**Expected:** Running `make generate-from-spec` would generate the router file at `services/backend/src/app/api/routers/todos.py` (like protocols and controllers are generated).
**What happened:** The framework generated `protocols.py` (updated with TodosControllerProtocol) and `controllers/todos.py` (stub), but did NOT generate the router file. The router had to be created manually.
**Impact:** The main `router.py` also needed manual updating to include the new todos router. This breaks the "spec-first" philosophy — if the spec is the source of truth, the router should be generated from it.
**Suggestion:** Either generate routers automatically, or clearly document that routers must be created manually and provide a template/example.

### 3. Controller stub generated with empty docstrings

**Severity:** Low
**Location:** `services/backend/src/controllers/todos.py` (generated)
**What happened:** Generated controller methods have `""" """` (space-only docstrings) instead of meaningful placeholders or no docstrings.
**Suggestion:** Either generate meaningful docstrings like `"""Handler for create_todo."""` or omit them entirely.

### 4. Generated protocol formatting has inconsistent indentation

**Severity:** Low
**Location:** `services/backend/src/generated/protocols.py`
**What happened:** The generated protocols.py has inconsistent indentation — parameters are indented with varying levels of whitespace. For example:
```python
async def create_todo(
    self,
    session: AsyncSession,
                    payload: TodoCreate,
        ) -> TodoRead:
```
**Expected:** Standard Python formatting:
```python
async def create_todo(
    self,
    session: AsyncSession,
    payload: TodoCreate,
) -> TodoRead:
```
**Impact:** While functional, it makes the generated code harder to read. The `ruff format` command doesn't touch generated files.

### 5. No `PATCH` method examples in existing specs or documentation

**Severity:** Low
**Location:** `services/backend/spec/users.yaml`, AGENTS.md
**What happened:** The existing user spec uses PUT for updates. The task explicitly requires PATCH for partial updates, which is the more RESTful choice. No existing examples of PATCH usage in the codebase.
**Suggestion:** Add a note in documentation that both PUT and PATCH are supported in specs.

### 6. Migration cannot be auto-generated natively

**Severity:** Medium
**Location:** Makefile `makemigrations` target
**What happened:** The `make makemigrations` target uses Docker Compose, which is not available in the native development environment. Had to create the migration file manually.
**Expected:** `make makemigrations EXEC_MODE=native` should work for generating migrations locally.
**Suggestion:** Add a native mode for `makemigrations` that runs alembic directly, similar to how `make tests` works with `EXEC_MODE=native`.

### 7. Generated `TodoUpdate` schema has default values that could confuse PATCH semantics

**Severity:** Low
**Location:** `shared/shared/generated/schemas.py`
**What happened:** The generated `TodoUpdate` model has:
```python
class TodoUpdate(BaseModel):
    title: str | None = None
    description: str | None = ""
    is_completed: bool | None = False
```
Fields `description` and `is_completed` have non-None defaults (`""` and `False`), which means `model_dump()` without `exclude_unset=True` would include them. This works correctly when using `exclude_unset=True` in the controller, but it's a subtle gotcha for developers.
**Suggestion:** For Update variants, consider generating all optional fields with `None` defaults to make partial update semantics clearer.

## What Worked Well

1. **Spec validation (`make validate-specs`)** — caught the invalid `response_list` field immediately with a clear error message.
2. **Code generation (`make generate-from-spec`)** — smoothly generated Pydantic schemas, protocols, and controller stubs from the YAML spec.
3. **Controller sync linting (`make lint-controllers`)** — verifies that controller implementations match protocol signatures.
4. **Spec compliance enforcement (`make lint-specs`)** — prevents manually creating BaseModel or APIRouter in controllers.
5. **Test infrastructure** — SQLite-based test fixtures with transaction rollback work well and are fast (~0.8s for 20 tests).
6. **Existing patterns** — The User CRUD implementation served as an excellent template for the Todo implementation.
7. **All linters and tests pass** — `make lint` and `make tests` both pass cleanly after implementation.

## Suggestions for Improvement

1. **Generate routers from specs** — This is the biggest gap in the spec-first workflow.
2. **Add a "list endpoint" example** to AGENTS.md or ARCHITECTURE.md.
3. **Add `EXEC_MODE=native` support** for `make makemigrations`.
4. **Fix formatting** in generated protocols.py templates.
5. **Add PATCH examples** to documentation alongside PUT.
6. **Consider a `make scaffold-domain` command** that creates the full stack (spec → generate → router → migration) in one step.

