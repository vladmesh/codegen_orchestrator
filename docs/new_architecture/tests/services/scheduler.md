# Testing Strategy: Scheduler

This document defines the testing strategy for the `scheduler` service.
The scheduler acts as a bridge between External Sources (GitHub, Time4VPS, Infra) and our Internal API. Use a dual-layered approach to verify both internal logic and external contracts.

## 1. Philosophy: Isolation & Integration

We employ two distinct test levels:

1.  **Component Tests (Isolation)**: We mock *everything* external (GitHub, Time4VPS, **and** Internal API).
    *   *Goal*: Ensure the scheduler correctly *parses* external data and *constructs* the correct internal API calls.
    *   *Speed*: Very Fast.

2.  **Integration Tests (Contract)**: We mock External APIs (stable data) but use the **Real Internal API Client** (connected to a real Test DB).
    *   *Goal*: Ensure the data we construct is actually accepted by the API (Schema check, logic check).
    *   *Speed*: Medium (requires DB transaction).

## 2. Test Pyramid

| Level | Scope | External APIs | Internal API | Focus |
|-------|-------|---------------|--------------|-------|
| **Component** | Logic only | Mocked (JSON) | Mocked (Respx) | Logic robustness, Error handling, Retries |
| **Integration** | Full Flow | Mocked (VCR/JSON) | **Real** (In-process) | Data persistence, Schema usage, DB state updates |

## 3. Test Infrastructure

### 3.1 External Mocks
*   **GitHub/Time4VPS**: We do NOT hit real external APIs.
*   **Technique**: Use `pytest-respx` to interpret HTTP calls and return static JSON fixtures from `tests/fixtures/github_responses/` etc.

### 3.2 Internal API Strategy
This is the key differentiator between the two levels.

*   **For Component Tests**:
    *   `api_client` is a Mock (MagicMock or Respx).
    *   Assert: `api_client.create_project.assert_called_with(...)`.

*   **For Integration Tests**:
    *   `api_client` is a real `httpx.AsyncClient` hitting the `api` service (mounted via `FastAPI` instance or running locally).
    *   The `api` service is connected to the `postgres_db` fixture (Transaction Rollback).
    *   Assert: `db_session.scalar(select(Project).where(...))` finds the record.

## 4. Test Scenarios

### 4.1 GitHub Sync (Logic/Component)
*   **Input**: Mock GitHub API returns a list of 3 repos (1 new, 1 existing, 1 ignored).
*   **Mock API Behavior**: Returns Success.
*   **Assert**:
    *   Scheduler calls `POST /api/projects` for the new repo.
    *   Scheduler does NOT call `POST` for the existing repo.
    *   Scheduler handles API 500 errors gracefully (logs error, continues to next repo).

### 4.2 GitHub Sync (integration)
*   **Input**: Mock GitHub API returns 1 new repo "codegen-demo".
*   **Real API**: Running against Test DB.
*   **Action**: scheduler runs `sync_github()`.
*   **Assert**:
    *   Query Test DB: `SELECT * FROM projects WHERE name='codegen-demo'` returns 1 row.
    *   The row has correct `github_url`, etc.

### 4.3 Health Checker (Paramiko Mocking)
*   **Input**: `get_servers()` internal mock returns 1 server "192.168.1.1".
*   **Mock SSH**:
    *   Command `docker ps` returns exit code 0.
    *   Command `df -h` returns "50% used".
*   **Action**: `check_health()`.
*   **Assert (Integration)**: `SELECT health_status FROM servers` is "healthy".

### 4.4 Provisioner Trigger
*   **Setup (Integration)**: Insert a Server with status `pending_setup` into Test DB.
*   **Action**: Scheduler runs `trigger_provisioning()`.
*   **Assert**:
    *   Message published to Redis `provisioner:queue` (check Redis stream).
    *   DB status remains `pending_setup` (until async result comes back).

## 5. Implementation Plan

1.  **Fixtures**:
    *   `github_mock`: Configurable respx router.
    *   `api_integration_client`: Real client + DB.
    *   `api_mock_client`: Fake client.

2.  **Directory Structure**:
    ```
    scheduler/tests/
    ├── component/          # Mocked API
    │   ├── test_github_sync.py
    │   └── test_health.py
    ├── integration/        # Real API
    │   ├── test_github_flow.py
    │   └── test_provisioning_flow.py
    └── fixtures/           # JSON files
        ├── github_repos.json
        └── vps_list.json
    ```
