# Testing Strategy: API

This document defines the testing strategy for the `api` service.
Since the `api` service acts as a "dumb" access layer to the database without complex business logic, we prioritize **Integration Tests** over Unit Tests.

## 1. Philosophy: Real DB, Clean State

We adhere to the following principles:
1.  **No Mocks for DB**: Mocks hide schema violations and SQL errors. We use a real PostgreSQL instance.
2.  **Transaction Rollback**: To ensure speed and isolation, every test runs inside a database transaction that is rolled back at the end.
3.  **Black Box HTTP**: We test primarily via `AsyncClient` hitting the FastAPI endpoints, treating the app largely as a black box.

## 2. Test Pyramid

| Level | Scope | Focus | Implementation |
|-------|-------|-------|----------------|
| **Integration** | `FastAPI` + `Postgres` | CRUD correctness, Schema validation, Constraints | `pytest-asyncio`, `httpx`, `SQLAlchemy` |
| **Unit** | Schemas / Utils | Complex Pydantic validation (rare) | Pure Python |

> **Note:** We do NOT implement E2E tests for the API service in isolation. E2E tests cover the API naturally when testing the full system (e.g., via `orchestrator-cli` or `telegram-bot`).

## 3. Test Infrastructure & Fixtures

### 3.1 `postgres_db` Fixture
*   Scope: `session` (created once per test run).
*   Action: Starts a Docker container with PostgreSQL (or connects to a local test DB).
*   Setup: Runs `alembic upgrade head` to ensure the schema matches the codebase.

### 3.2 `db_session` Fixture
*   Scope: `function` (created for every test).
*   Action:
    1.  Connects to `postgres_db`.
    2.  Starts a nested transaction (`connection.begin_nested()`).
    3.  Yields the `AsyncSession`.
    4.  **Rolls back** the transaction on exit (`await transaction.rollback()`).

### 3.3 `client` Fixture
*   Scope: `function`.
*   Action: Creates an `httpx.AsyncClient` connected to the FastAPI app, injecting the isolated `db_session`.

## 4. Test Scenarios

### 4.1 Basic CRUD (Happy Path)
Ensure we can create, read, update, and delete entities.

**Example: Projects**
1.  `POST /api/projects`: Create a project with valid data. Assert 200 OK and correct JSON return.
2.  `GET /api/projects/{id}`: Fetch the created project. Assert data matches.
3.  `GET /api/projects`: List projects. Assert the new project is in the list.

### 4.2 Validation (Pydantic)
Ensure invalid data is rejected with 422 Unprocessable Entity.

**Example:**
*   Send `POST /api/projects` with missing `name`.
*   Assert status `422`.
*   Assert error message mentions the missing field.

### 4.3 Database Constraints
Ensure the DB protects data integrity.

**Example: Unique Constraints**
1.  Create Project A with name "demo".
2.  Try to create Project B with name "demo" (if name must be unique).
3.  Assert status `409 Conflict` (mapped from `IntegrityError`).

**Example: Foreign Keys**
1.  Try to create a Task linked to a non-existent Project ID.
2.  Assert status `404 Not Found` or `400 Bad Request` (depending on implementation preference, but never 500).

## 5. Implementation Guidelines

### 5.1 Directory Structure
```
api/tests/
├── conftest.py          # Fixtures (db, client)
├── factories.py         # Polyfactory/Model Bakery factories
├── integration/
│   ├── test_projects.py
│   ├── test_tasks.py
│   ├── test_servers.py
│   └── ...
└── unit/                # Optional, only for complex utils
    └── test_utils.py
```

### 5.2 Factories
Use `polyfactory` or similar to generate test data. Do NOT hardcode JSON dictionaries in tests.

```python
# factories.py
class ProjectFactory(ModelFactory[Project]):
    __model__ = Project
```

```python
# test_projects.py
async def test_create_project(client):
    payload = ProjectFactory.build_dict()
    response = await client.post("/api/projects", json=payload)
    assert response.status_code == 200
```

## 6. Access Control Testing
The API is internal but has basic header checks.

*   **Scenario:** Request without `X-Telegram-ID` (if required).
*   **Assert:** `401 Unauthorized` or `403 Forbidden`.
