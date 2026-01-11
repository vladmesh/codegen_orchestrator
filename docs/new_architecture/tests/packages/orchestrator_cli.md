# Testing Strategy: Orchestrator CLI

**Package:** `packages/orchestrator-cli`
**Focus:** Reliability of the Agent-to-System interface.

## 1. Philosophy

The CLI is an **API for Agents**. It must never confuse the agent with stack traces or ambiguous output.
- **Input:** structured arguments (verified by `typer`).
- **Output:** strict JSON (verified by tests).
- **Execution:** Atomic operations (API create + Redis publish).

## 2. Test Pyramid

| Level | Granularity | Scope | Focus | Coverage Target |
|-------|-------------|-------|-------|-----------------|
| **Unit** | Function/Module | Logic only | Permissions, JSON formatting, Argument parsing | ~60% |
| **Integration** | Command | CLI + Mock API/Redis | End-to-end command flow, Error handling | ~40% |

## 3. Detailed Plans

### 3.1 Unit Tests (Logic)

**Key Scenarios:**

*   **Permission Enforcement (`permissions.py`):**
    *   *Allow:* Command in `ALLOWED_COMMANDS` -> pass.
    *   *Deny:* Command NOT in `ALLOWED_COMMANDS` -> raise `PermissionError`.
    *   *Wildcard:* `project.*` allows `project.create`.
    *   *Empty:* No env var -> allows everything (or default policy).

*   **Response Formatting:**
    *   Verify `format_output(data)` produces valid JSON.
    *   Verify `format_error(exception)` produces valid JSON with `success: false`.

### 3.2 Integration Tests (Mocked External)

These tests run the actual CLI entry point but intercept network calls.

**Tools:**
*   `typer.testing.CliRunner`: To invoke commands.
*   `respx` or `pytest-httpx`: To mock REST API calls.
*   `fakeredis`: To mock Redis publications.

**Scenarios:**

*   **`project create` (Success):**
    1.  Mock `POST /api/projects` -> 200 OK `{id: "123"}`.
    2.  Run `orchestrator project create --name test`.
    3.  Assert exit code 0.
    4.  Assert stdout is valid JSON containing `"id": "123"`.

*   **`engineering start` (Dual Write):**
    1.  Mock `POST /api/tasks` -> 200 OK `{id: "task-1"}`.
    2.  Run `orchestrator engineering start --project 123 --spec "do it"`.
    3.  Assert API was called.
    4.  Assert message published to `engineering:queue` in fakeredis.
    5.  Assert stdout contains task ID.

*   **API Error Handling:**
    1.  Mock `POST /api/projects` -> 500 Server Error.
    2.  Run command.
    3.  Assert exit code 0 (Agents prefer formatted errors over non-zero exits, or strictly controlled exit codes). *Discussion needed: should CLI exit 1 on API error? Probably yes, but output MUST be JSON.*
    4.  Assert stdout contains `{"success": false, "message": "Internal Server Error"}`.

*   **Permission Denied:**
    1.  Set env `ALLOWED_COMMANDS="project.list"`.
    2.  Run `orchestrator project create`.
    3.  Assert stdout contains `PermissionDenied`.

## 4. Test Infrastructure

*   **Fixtures:**
    *   `mock_api`: Respx router.
    *   `mock_redis`: Fakeredis client.
    *   `cli_runner`: Typer runner.

## 5. Migration Validation

*   Since we are extracting this from `shared/cli`, we must ensure:
    *   All imports are relative to the new package structure.
    *   `entry_points` in `pyproject.toml` are correctly configured.
