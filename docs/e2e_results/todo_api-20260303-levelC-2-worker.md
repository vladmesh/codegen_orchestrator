# Audit Report

## Framework & Development Environment Audit

### Bug: Generated code uses `uuid` (module) instead of `UUID` (type) for param types

**Severity**: High
**Location**: `services/backend/src/generated/protocols.py`, auto-generated controller scaffold
**What happened**: When a domain spec uses `type: uuid` for a param (e.g., `todo_id`), the code generator emits `todo_id: uuid` in both the protocol and the controller scaffold. Here `uuid` refers to the Python module, not the `UUID` class from `from uuid import UUID`. This produces invalid type annotations.
**Expected**: The generator should emit `todo_id: UUID` and add `from uuid import UUID` to the imports.
**Workaround**: The controller file (`src/controllers/todos.py`) is editable so I manually added `import uuid` and used `uuid.UUID` as the type. However, the protocol file (`src/generated/protocols.py`) is read-only (regenerated), so the incorrect `uuid` type annotation persists there. This works at runtime because Python Protocol checks are structural and duck-typed, but it would fail `mypy --strict` type checking.
**File**: `services/backend/src/generated/protocols.py:44-58` — three occurrences of `todo_id: uuid` should be `todo_id: UUID`.

### Issue: Test isolation with SQLite — data leaks across tests

**Severity**: Medium
**Location**: `services/backend/tests/conftest.py`
**What happened**: The test fixtures use SQLite with `begin_nested()` savepoints, but data created in one test is visible in subsequent tests. For example, a test that creates two todos and then lists them expects exactly 2 results, but gets 8 because prior tests' data persisted.
**Expected**: Each test should start with a clean database state. The `db_session` fixture uses a transaction + rollback pattern, but it appears the rollback doesn't fully clean up between tests — likely because `begin_nested()` in the `_get_test_db` override commits the savepoint, and the outer transaction sees those changes.
**Workaround**: Wrote tests to be resilient to pre-existing data (e.g., count before + count after instead of asserting absolute counts).

### Observation: Documentation is in Russian

**Severity**: Low
**Location**: `AGENTS.md`, `services/backend/AGENTS.md`, `CONTRIBUTING.md`, `ARCHITECTURE.md`
**What happened**: All framework documentation is in Russian. This limits accessibility for non-Russian-speaking developers.
**Suggestion**: Consider providing English translations or at least an English summary.

### Observation: Generated schemas use `Optional` pattern inconsistently

**Severity**: Low
**Location**: `shared/shared/generated/schemas.py`
**What happened**: The generated `TodoCreate` schema has `description: str | None = ""` and `is_completed: bool | None = False`. The `| None` is unnecessary when a non-None default is provided — the field can never actually be `None` in normal usage. This could lead to confusion where a consumer sends `{"description": null}` and it gets accepted.
**Suggestion**: When a field has a non-None default, don't make it `Optional`. Generate `description: str = ""` instead of `description: str | None = ""`.

### Observation: `make tests` command — EXEC_MODE documentation gap

**Severity**: Low
**Location**: `CLAUDE.md`, `AGENTS.md`
**What happened**: The instructions mention `EXEC_MODE=native` for running tests natively, but the `Makefile` doesn't actually use the `EXEC_MODE` variable — the `tests` target always runs using the service's local `.venv/bin/pytest`. The `EXEC_MODE=native` parameter is a no-op for test commands.
**Expected**: Either the Makefile should respect `EXEC_MODE`, or the documentation should not require it for `make tests`.

### Positive: Spec-first workflow works well

The spec → generate → implement cycle is smooth. Defining models in `models.yaml` and operations in `spec/todos.yaml`, then running `make generate-from-spec` correctly produced:
- Pydantic schemas with proper UUID support
- Protocol definitions (apart from the `uuid` type bug)
- Controller scaffold with all methods stubbed out

The `make validate-specs` and `make lint-controllers` checks are valuable for catching spec/code drift.

### Positive: Infrastructure management

`orchestrator dev-env start-infra db` worked smoothly — started PostgreSQL, waited for health check, and the database was immediately usable for migrations. Clean experience.

### Positive: Alembic migration autogeneration

`make makemigrations` correctly detected the new `todos` table with all columns including UUID primary key with `gen_random_uuid()` server default. No manual migration editing was needed.

### Summary

| Category | Count |
|----------|-------|
| Bugs | 1 (UUID type in codegen) |
| Medium issues | 1 (test isolation) |
| Low issues | 3 (docs language, schema optionals, EXEC_MODE docs) |
| Positives | 3 (spec workflow, infra mgmt, migration autogen) |

