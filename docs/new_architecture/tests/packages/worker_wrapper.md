# Testing Strategy: Worker Wrapper

**Package:** `packages/worker-wrapper`
**Focus:** Reliability of the "nervous system" that bridges Redis and the Agent.

## 1. Philosophy

The Worker Wrapper runs in a constrained environment (inside a Docker container, potentially with limited tools). It must be robust against:
1.  Agent process crashes/hangs.
2.  Malformed LLM output (the most common failure mode).
3.  Redis network blips.

We do **not** use full E2E tests here. We use **Heavy Unit Tests** for logic and **Component Integration Tests** (Mocked) for process/IO control.

## 2. Test Pyramid

| Level | Granularity | Scope | Focus | coverage Target |
|-------|-------------|-------|-------|-----------------|
| **Unit** | Class/Function | Logic only | Input validation, Output parsing, Command construction | ~70% |
| **Component** | Process | Wrapper + Redis + Mock Agent | Process lifecycle, Redis protocol, Timeout handling | ~30% |

## 3. Detailed Plans

### 3.1 Unit Tests (Logic)

These tests run in isolation with no IO.

**Key Scenarios:**

*   **Output Parsing (`ResultParser`):**
    *   *Happy Path:* Extract content from `<result>{...}</result>`.
    *   *Dirty Input:* Handle extra whitespace, newlines inside tags, text outside tags.
    *   *Broken Input:* Missing closing tag, invalid JSON inside tags -> raise `WorkerError` (do not crash).
    *   *Streaming:* (If supported) partial parsing state.

*   **Message Validation:**
    *   Validate `WorkerInputMessage` from various valid/invalid dictionaries.
    *   Ensure defaults (e.g., `timeout`) are applied correctly.

*   **Command Builder:**
    *   Verify `ClaudeAgent` builds correct CLI args: `['claude', '--headless', '--session', '...']`.
    *   Verify `FactoryAgent` builds correct CLI args.
    *   Check environment variable injection (API keys, etc.).

### 3.2 Component Integration Tests (Internal I/O)

These tests verify the wrapper's ability to control a subprocess and talk to Redis.

**Setup:**
*   **Local Redis**: Run in a service container or use `fakeredis`.
*   **Mock Agent Script**: A simple python script (`tests/mocks/mock_agent.py`) that simulates the agent binary.

**Mock Agent Behaviors (controlled by env var):**
1.  **ECHO**: Prints input arguments and a valid JSON result to stdout.
2.  **SLEEP**: Sleeps for N seconds (to test timeouts).
3.  **CRASH**: Exits with non-zero code.
4.  **GARBAGE**: Prints random text without `<result>` tags.

**Test Scenarios:**

*   **Full Cycle (Happy Path):**
    1.  Push message to `worker:developer:test:input`.
    2.  Run `worker_wrapper` (pointing `AGENT_BINARY` to `mock_agent.py` in ECHO mode).
    3.  Assert `worker:developer:test:output` contains success result.

*   **Timeout Handling:**
    1.  Push message with `timeout=1`.
    2.  Run wrapper with Mock Agent in SLEEP(5) mode.
    3.  Assert wrapper kills the process.
    4.  Assert `output` contains `status: "timeout"`.

*   **Process Crash:**
    1.  Run wrapper with Mock Agent in CRASH mode.
    2.  Assert `output` contains `status: "error"` and captures stderr.

*   **Redis Disconnect (Optional):**
    1.  Start wrapper.
    2.  Kill Redis.
    3.   Verify wrapper attempts reconnect or exits gracefully (depending on design).

## 4. Test Tools

*   `pytest`: Test runner.
*   `pytest-asyncio`: For async entry points.
*   `fakeredis`: Preferable to real Redis for speed, unless XREAD blocking behavior differs significantly.
*   `subprocess`: Use real subprocess calls to `mock_agent.py` to verify OS-level signal handling (SIGTERM/SIGKILL).
