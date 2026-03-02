# Audit Report

## Overview
Audit performed while implementing the Todo API (GET/POST/PATCH/DELETE /todos) on the service-template framework.

## Issues Encountered

### 1. Database Infrastructure: Password Authentication Failure
**Severity**: High
**Location**: `orchestrator dev-env start-infra db`

When running `orchestrator dev-env start-infra db`, the PostgreSQL container starts but **password authentication fails** for the `postgres` user with the password defined in `.env` (`POSTGRES_PASSWORD=postgres`).

The `infra/compose.base.yml` defines:
```yaml
db:
  environment:
    POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-postgres}
```

However, the orchestrator does not appear to pass the `.env` file variables to Docker Compose for variable substitution in the compose file itself. The container logs show PostgreSQL initializing with "trust" authentication for local connections only, suggesting `POSTGRES_PASSWORD` was empty/unset during initialization.

**Impact**: Unable to use `make makemigrations` or `make migrate` natively. Had to write the migration file manually.
**Expected**: Running `orchestrator dev-env start-infra db` should produce a database accessible with the credentials in `.env`.
**Workaround**: Wrote migration manually instead of using `make makemigrations name="..."`.

### 2. `.env` Not Auto-Loaded for Native Make Targets
**Severity**: Medium
**Location**: `Makefile`

The Makefile includes `-include .env` and `export`, which should load `.env` for Make targets. However, when running `make makemigrations EXEC_MODE=native`, the Alembic process fails because the settings module (`services/backend/src/core/settings.py`) cannot find required env vars (`APP_NAME`, `APP_ENV`, etc.).

The issue is that `services/backend/src/core/settings.py` validates env vars in `_validate_required_env_vars()`, but the `.env` file's `POSTGRES_HOST=project-db` is only valid inside Docker networks. For native execution, it should be `db` or `localhost`.

**Expected**: A clear way to run migrations natively with correct env vars.
**Suggestion**: Provide an `.env.native` or document that `POSTGRES_HOST` must be overridden for native execution.

### 3. Controller Stub Generation is Excellent
**Severity**: N/A (Positive)
**Location**: `make generate-from-spec`

The framework's code generation from YAML specs works very well:
- Generated Pydantic schemas with correct variants (Create, Update, Read)
- Generated protocol with all CRUD operations
- Generated controller stub with correct method signatures
- Existing controllers (users) are preserved during regeneration

### 4. AGENTS.md Documentation in Russian
**Severity**: Low
**Location**: `AGENTS.md`, `services/backend/AGENTS.md`

The documentation is primarily in Russian, which may be a barrier for non-Russian-speaking developers or AI agents. The examples (code snippets) are clear, but the surrounding text requires translation.

**Suggestion**: Consider providing English translations alongside Russian text, or use English as the primary documentation language for wider accessibility.

### 5. `orchestrator dev-env compose -- exec` Limited
**Severity**: Low
**Location**: `orchestrator dev-env compose`

Running `orchestrator dev-env compose -- exec db env` to inspect container environment returns a 400 error. This makes debugging infrastructure issues harder.

**Error**: `Error: Client error '400 Bad Request' for url 'http://worker-manager:8000/api/worker/.../infra/compose'`

### 6. Test Infrastructure Works Well
**Severity**: N/A (Positive)

The test infrastructure using SQLite + aiosqlite for unit tests is well-designed:
- `conftest.py` provides clean session isolation per test
- Mock broker prevents Redis dependency
- Tests run fast (~1.5s for 20 tests)

## Suggestions for Improvement

1. **Fix orchestrator env var passing**: The orchestrator should load `.env` and pass variables to Docker Compose for variable substitution in compose files.
2. **Add native development docs**: Document how to run migrations and other DB-dependent operations natively (outside Docker).
3. **Add `.env.test` auto-detection**: The Makefile could detect test mode and use a separate `.env.test` file.
4. **Translate docs to English**: Consider bilingual documentation for wider accessibility.
5. **Add a `make migrate` target**: For applying migrations natively (with proper env var handling).
