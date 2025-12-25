# Test Infrastructure

This document describes the test structure and how to run tests for the Codegen Orchestrator project.

## Directory Structure

Tests are organized per service with separation between unit and integration tests:

```
services/
  api/
    tests/
      unit/              # Fast tests, no external dependencies
      integration/       # Tests requiring DB, Redis, etc.
    Dockerfile.test      # Optimized test image
    pytest.ini           # Service-specific pytest config
  
  langgraph/
    tests/
      unit/
      integration/
    Dockerfile.test
    pytest.ini
  
  scheduler/
    tests/
      unit/
      integration/
    Dockerfile.test
    pytest.ini
  
  telegram_bot/
    tests/
      unit/
    Dockerfile.test
    pytest.ini

tests/
  e2e/                   # End-to-end system tests (future)
```

## Test Types

### Unit Tests
- **Location**: `services/{service}/tests/unit/`
- **Purpose**: Test individual functions, classes, and modules in isolation
- **Dependencies**: None (mocked external services)
- **Speed**: Very fast (< 1 second per test)
- **Run with**: `make test-{service}-unit`

### Integration Tests
- **Location**: `services/{service}/tests/integration/`
- **Purpose**: Test components working together with real dependencies
- **Dependencies**: Database, Redis, or other services
- **Speed**: Slower (1-10 seconds per test)
- **Run with**: `make test-{service}-integration`

### E2E Tests (Future)
- **Location**: `tests/e2e/`
- **Purpose**: Test full user workflows across all services
- **Dependencies**: Full stack running
- **Speed**: Slowest (10+ seconds per test)

## Running Tests

### Quick Commands

```bash
# Run all unit tests (fast, recommended for development)
make test-unit

# Run all integration tests
make test-integration

# Run ALL tests
make test-all

# Run tests for a specific service
make test-api
make test-langgraph
make test-scheduler
make test-telegram
```

### Granular Commands

```bash
# API service
make test-api-unit          # API unit tests only
make test-api-integration   # API integration tests only
make test-api               # All API tests

# LangGraph service
make test-langgraph-unit
make test-langgraph-integration
make test-langgraph

# Scheduler service
make test-scheduler-unit
make test-scheduler-integration
make test-scheduler

# Telegram Bot service
make test-telegram-unit
make test-telegram
```

### Cleanup

```bash
# Remove test containers and volumes
make test-clean
```

## Docker Test Images

Each service has a dedicated `Dockerfile.test` with multi-stage builds:

1. **base**: System dependencies
2. **test-deps**: Python dependencies (cached layer)
3. **test**: Source code + test files

This structure maximizes Docker layer caching:
- Dependencies are installed once and cached
- Only source code changes trigger rebuilds
- Tests run 5-10x faster on subsequent runs

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
from sqlalchemy.ext.asyncio import create_async_engine

@pytest.mark.asyncio
async def test_database_connection():
    """Test that we can connect to the test database."""
    engine = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@db-test:5432/orchestrator_test"
    )
    async with engine.connect() as conn:
        result = await conn.execute("SELECT 1")
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
addopts = -v --strict-markers --cov=src
```

## CI/CD Integration

Tests are designed to work efficiently in CI:

- **Fast feedback**: Unit tests run first and fail fast
- **Parallel execution**: Services can be tested in parallel
- **Differential testing**: Only test changed services
- **Docker caching**: Leverage GitHub Actions cache for Docker layers

Example CI workflow:

```yaml
jobs:
  test-unit:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        service: [api, langgraph, scheduler, telegram]
    steps:
      - uses: actions/checkout@v4
      - name: Run unit tests
        run: make test-${{ matrix.service }}-unit
```

## Best Practices

1. **Unit tests should be fast**: < 1 second per test
2. **Mock external dependencies**: Use `unittest.mock` or `pytest-mock`
3. **Integration tests should be isolated**: Each test should clean up after itself
4. **Use fixtures**: Share common setup code via pytest fixtures
5. **Mark your tests**: Use `@pytest.mark.unit` or `@pytest.mark.integration`
6. **Test one thing**: Each test should verify one specific behavior
7. **Use descriptive names**: `test_user_creation_fails_without_email` not `test_user_1`

## Troubleshooting

### Tests are slow
- Run only unit tests: `make test-unit`
- Run tests for one service: `make test-api-unit`
- Check Docker layer caching is working

### Import errors
- Ensure `PYTHONPATH` is set correctly in `docker-compose.test.yml`
- Check that all `__init__.py` files exist

### Database connection errors
- Ensure test infrastructure is running: `docker compose -f docker-compose.test.yml up -d db-test`
- Check database healthchecks are passing

### Permission errors
- Test volumes are mounted read-only (`:ro`)
- Don't try to write to mounted directories during tests
