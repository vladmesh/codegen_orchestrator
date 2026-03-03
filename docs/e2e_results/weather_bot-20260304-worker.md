# Audit Report

## Environment Setup

### Issue: `make setup` fails if `.venv` already exists
- **What happened**: Running `make setup` failed with `error: Failed to create virtual environment. A virtual environment already exists at /workspace/.venv. Use --clear to replace it`.
- **Expected**: `make setup` should be idempotent and succeed even if `.venv` already exists.
- **Workaround**: Ran `uv venv --clear` manually, then installed dependencies manually.
- **Suggestion**: Change `uv venv` to `uv venv --clear` in the Makefile setup target, or add a check.

## Spec-First Workflow

### Positive: Code generation works well
- Adding `WeatherResponse` to `shared/spec/models.yaml` and creating `services/backend/spec/weather.yaml` generated schemas, protocols, and controller stubs correctly.
- `make validate-specs` caught issues early and gave clear output.
- `make generate-from-spec` correctly generated `WeatherControllerProtocol`, `WeatherResponse` schema, and a controller stub.

### Observation: Controller generation only creates stubs
- The generated controller in `services/backend/src/controllers/weather.py` contained only `raise NotImplementedError(...)`. This is expected behavior but worth noting — the developer must implement all logic.

### Positive: `lint-controllers` checks protocol compliance
- Running `make lint` verified that the controller implements all protocol methods correctly. This is a helpful safety net.

## Linting and Complexity

### Issue: Xenon excludes only root `tests/*`, not service test directories
- **What happened**: `services/backend/tests/unit/test_weather.py` was flagged by xenon for module complexity rank B, despite test files being logically in a "tests" directory.
- **Expected**: Service test directories (`services/*/tests/`) should be excluded from xenon complexity checks, same as root-level `tests/*`.
- **Root cause**: The Makefile uses `--exclude '.framework/*,tests/*'` which only matches the root `tests/` directory, not `services/backend/tests/`.
- **Workaround**: Reduced the number of assertions per test function to bring complexity to rank A.
- **Suggestion**: Update xenon exclude pattern to also cover service tests: `--exclude '.framework/*,tests/*,services/*/tests/*'`. Alternatively, test files generally shouldn't be held to the same complexity standards as production code.

### Observation: `assert` counts as cyclomatic complexity branch
- Python's `assert` is treated as a branch by radon/xenon. This means test functions with many assertions quickly hit complexity limits. This is a known limitation of radon.

## Database Migrations

### Issue: `make makemigrations` requires running PostgreSQL
- **What happened**: Cannot use `make makemigrations` to auto-generate migrations without a running PostgreSQL instance.
- **Expected**: Would be convenient to generate migrations without requiring infrastructure.
- **Workaround**: Created the migration file manually, following the pattern from existing migration `118f8b3895d8_create_user.py`.
- **Suggestion**: Consider adding a way to generate migrations using SQLite for development, or document this requirement more prominently.

## Telegram Bot

### Observation: `BackendClient` base_url_env uses `BACKEND_API_URL`
- The tg_bot AGENTS.md documents the env var as `API_BASE_URL`, but the actual code in `main.py` uses `BACKEND_API_URL`. This inconsistency could confuse developers.
- **Suggestion**: Align the AGENTS.md documentation with the actual code, or vice versa.

## Framework Observations

### Positive: Shared schema generation
- The `shared/shared/generated/schemas.py` properly generates Pydantic models from YAML specs with `extra="forbid"` for strict validation.

### Positive: Event system
- The event broker pattern with lazy initialization via `get_broker()` is clean and works well.

### Positive: Project structure
- The separation between generated code and implementation code (protocols vs controllers) is well-organized.
- The `ServiceClient` base class with retry logic is useful for inter-service communication.

### Minor: Double-nested `shared/shared/` package
- While documented in AGENTS.md as a "standard Python packaging convention", this can still be confusing for newcomers. The explanation helps but the convention itself is unusual.

## Summary

The framework is generally well-designed and productive. The main pain points are:
1. `make setup` not being idempotent
2. Xenon complexity excludes not covering service test directories
3. Migration generation requiring running PostgreSQL
4. Minor documentation inconsistency in tg_bot AGENTS.md env var names

