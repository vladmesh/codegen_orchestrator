# Test Infrastructure

This document describes the test structure and how to run tests for the Codegen Orchestrator project.

## Directory Structure

Tests are organized per service with separation between unit and integration tests:

```
services/
  api/tests/{unit,integration}/
  langgraph/tests/{unit,integration}/
  worker-manager/tests/{unit,integration}/
  infra-service/tests/{unit,integration}/
  scheduler/tests/{unit,integration}/
  telegram_bot/tests/unit/

tests/
  unit/         # Cross-service unit tests (worker-manager, etc.)
  integration/  # Cross-service integration tests
  e2e/          # End-to-end system tests
```

## Test Types

### Unit Tests
- **Location**: `services/{service}/tests/unit/` and `tests/unit/`
- **Purpose**: Test individual functions, classes, and modules in isolation
- **Dependencies**: None (mocked external services)
- **Speed**: Very fast (< 1 second per test)

### Integration Tests
- **Location**: `services/{service}/tests/integration/` and `tests/integration/`
- **Purpose**: Test components working together with real dependencies
- **Dependencies**: Database, Redis, or other services
- **Speed**: Slower (1-10 seconds per test)

### E2E Tests
- **Location**: `tests/e2e/` and `.claude/skills/e2e-*`
- **Purpose**: Test full user workflows across all services
- **Dependencies**: Full stack running + Real LLM
- **Run with**: `/e2e-run` skill (Level A/B/C)

## Running Tests

### Commands

```bash
# All unit tests across all services (fast, no deps)
make test-unit

# All integration tests (require DB/Redis running)
make test-integration

# Service-specific unit tests
make test-api-unit
make test-langgraph-unit
make test-scheduler-unit
make test-telegram-unit

# Service-specific integration tests
make test-api-integration
make test-langgraph-integration
make test-scheduler-integration

# E2E scaffold test (validates copier + make setup in worker container)
make test-e2e-scaffold

# Cleanup test containers and volumes
make test-clean
```

### Pre-push Hook

The pre-push hook runs automatically:
1. `make lint` — ruff check
2. `make test-unit` — all unit tests

Both must pass before push is allowed.

## Writing Tests

### Unit Test Example

```python
# services/api/tests/unit/test_models.py
def test_project_creation():
    """Test that a project can be created with valid data."""
    from src.models.project import Project

    project = Project(name="test", status="pending")
    assert project.name == "test"
```

### Integration Test Example

```python
# services/api/tests/integration/test_database.py
import pytest

@pytest.mark.asyncio
async def test_database_connection(db_session):
    """Test that we can connect to the test database."""
    result = await db_session.execute("SELECT 1")
    assert result.scalar() == 1
```

## Test Configuration

Each service has its own `pytest.ini`:

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
markers =
    unit: Unit tests
    integration: Integration tests
```

## CI/CD Integration

- **PR**: Unit tests + integration tests + build (no push)
- **Main**: Full test suite + image build + push to registry
- Pre-push hook ensures lint + unit tests pass locally before push

## Best Practices

1. **Unit tests should be fast**: < 1 second per test
2. **Mock external dependencies**: Use `unittest.mock` or `pytest-mock`
3. **Integration tests should be isolated**: Each test should clean up after itself
4. **Use fixtures**: Share common setup code via pytest fixtures
5. **Test one thing**: Each test should verify one specific behavior
6. **Use descriptive names**: `test_user_creation_fails_without_email` not `test_user_1`

## Troubleshooting

### Tests are slow
- Run only unit tests: `make test-unit`
- Run tests for one service: `make test-api-unit`

### Import errors
- Ensure `PYTHONPATH` includes service `src/` directory
- Check that all `__init__.py` files exist

### Database connection errors (integration tests)
- Ensure services are running: `make up`
- Check database healthchecks: `docker compose ps`
