# Audit Report

## Environment & Setup Issues

### 1. `make install` fails without `--system` or `--user` flag
- **File**: `Makefile:7`
- **Command**: `cd $(SERVICE_DIR) && uv pip install -e ".[dev]"`
- **Error**: `error: No virtual environment found; run 'uv venv' to create an environment, or pass '--system' to install into a non-virtual environment`
- **Workaround**: Used `pip install --user -e ".[dev]"` directly. The Makefile's `install` target assumes a virtualenv exists, but no venv is pre-created in the workspace.
- **Suggestion**: Either pre-create a virtualenv in the workspace, or change the Makefile to use `uv pip install --system` or `pip install --user`.

### 2. `pip install` (without `--user`) fails with permission denied
- **Error**: `OSError: [Errno 13] Permission denied: '/usr/local/lib/python3.12/site-packages/...'`
- **Context**: The workspace runs as a non-root user without write access to the system site-packages.
- **Workaround**: Had to use `pip install --user`.

### 3. User site-packages not on default Python path
- **Expected**: `pip install --user` packages should be importable by default.
- **Actual**: After `pip install --user`, `python -m pytest` failed with `No module named pytest`. The user site-packages directory (`/home/worker/.local/lib/python3.12/site-packages`) was not in `sys.path`.
- **Workaround**: Had to set `PYTHONPATH="/home/worker/.local/lib/python3.12/site-packages:$PYTHONPATH"` before running commands.
- **Suggestion**: Either pre-install dependencies in the system site-packages, create a virtualenv, or ensure user site-packages is on the default path. This is a friction point for every developer starting work.

### 4. Makefile `tests` target doesn't set PYTHONPATH
- **File**: `Makefile:19`
- **Issue**: `make tests EXEC_MODE=native` runs `python -m pytest` but doesn't account for user-installed packages not being on `sys.path`.
- **Suggestion**: Makefile targets should either use a virtualenv or set appropriate environment variables.

## Code Quality Issues Found in Previous Implementation

### 5. Field naming mismatch between task spec and implementation
- **Task spec**: Fields listed as `id, title, description, is_completed, created_at`
- **Implementation (models.yaml, model, schema, routes)**: Used `completed` instead of `is_completed`
- **Impact**: API response field name didn't match the spec. This was a naming inconsistency between the task requirements and the scaffolded `models.yaml`.
- **Fix applied**: Renamed `completed` to `is_completed` across all layers (model, schema, repository, service, routes, tests).

### 6. Repository update method silently skipped None values
- **File**: `services/backend/src/repositories/todo.py:41-44`
- **Issue**: The `update()` method had `if value is not None: setattr(todo, key, value)` which meant nullable fields like `description` could never be cleared (set to `None`) via a PATCH request.
- **Fix applied**: Removed the `is not None` guard. The service layer already uses `model_dump(exclude_unset=True)` to filter out fields that weren't explicitly sent, so only explicitly-provided values (including `None`) are passed to the repository.
- **Test added**: `test_update_todo_clear_description` to verify nullable fields can be cleared.

## Framework & Template Observations

### 7. No AGENTS.md present
- **Expected**: CLAUDE.md and TASK.md reference `AGENTS.md` for code structure patterns and conventions.
- **Actual**: No `AGENTS.md` file exists in the repository.
- **Impact**: Previous developer had to infer patterns without guidance. The template should either include an AGENTS.md or not reference it.

### 8. No code generation configured
- **File**: `Makefile:9-10`
- **Issue**: `make generate` just prints `"Spec files are in shared/spec/ - no code generation configured"`. The TASK.md instructs to "Run `make generate-from-spec` after modifying spec files to regenerate code", but there's no `generate-from-spec` target either.
- **Suggestion**: Either implement actual code generation from the spec files, or remove the misleading instruction from TASK.md. Currently the `shared/spec/models.yaml` is informational only and not enforced by tooling.

### 9. Copier scaffolding not available
- **Context**: TASK.md mentions "The project was scaffolded with `copier` from `service-template`" but the previous developer noted copier was not available and had to create the structure manually.
- **Impact**: Manual structure creation means potential deviation from the intended template patterns.

## Suggestions for Improvement

1. **Pre-configure a working Python environment**: Either use a virtualenv or ensure system Python has the right paths configured. The current setup requires manual workarounds for every developer.
2. **Add a `generate-from-spec` Make target**: If models.yaml is meant to drive code generation, implement it. Otherwise, remove references to it.
3. **Include AGENTS.md in the template**: Provide clear patterns for the layered architecture (models, schemas, repositories, services, routes).
4. **Validate spec consistency**: The task spec field names should be consistent with models.yaml. Either auto-generate models.yaml from the task spec or validate them against each other.
5. **Add a health check endpoint**: The API has no `/health` or readiness endpoint, which is standard for production services.

