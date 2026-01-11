# Service: LangGraph Orchestrator

**Service Name:** `langgraph`
**Responsibility:** Workflow Orchestration & State Management.

## 1. Philosophy: The "Thin" Orchestrator

The `langgraph` service is the "brain" that coordinates high-level processes, but it does **not** perform heavy work itself. It adheres to strict separation of concerns:

> **Rule #1:** LangGraph nodes never execute business logic directly (no running git, no ansible, no generation).
> **Rule #2:** Nodes are "Client Wrappers" that delegate work to Workers or Signals to other Services.
> **Rule #3:** Minimal Dependencies. The image should barely contain more than `langgraph`, `redis`, and `pydantic`.

## 2. Responsibilities

1.  **State Management**: Holding the specific state of long-running workflows (Checkpoints).
2.  **Routing**: Deciding "What happens next?" based on results (Conditional Edges).
3.  **Delegation**: Sending commands to specialized actors (`worker-manager`, `infra-service`).
4.  **Abstraction**: Exposing complex processes (like "Engineering") as single callable Subgraphs.

## 3. Architecture

### 3.1 Subgraphs as Abstractions

We model business domains as **Subgraphs**. External agents (like the Product Owner) simply "Call" a subgraph with inputs and wait for strict outputs, without knowing the internal topology.

#### A. Engineering Subgraph
*   **Goal**: Deliver tested code.
*   **Abstraciton**: `Repo + Spec` -> `Commit SHA`.
*   **Internal Flow**: `Developer` (write code) -> `Tester` (verify) -> `Loop`.
*   **Implementation**:
    *   `Developer` node DOES NOT write code.
    *   It sends `create_worker(type=CLAUDE, task=write_code)` to `worker-manager`.
    *   It waits for `worker:response`.

#### B. DevOps Subgraph
*   **Goal**: Make the project live.
*   **Abstraction**: `Project ID` -> `URL`.
*   **Internal Flow**: `Analyzer` -> `SecretResolver` -> `Deployer`.
*   **Implementation**:
    *   `Deployer` node DOES NOT run Ansible.
    *   It sends a job to `ansible:deploy:queue`.
    *   It waits for result in `deploy:results`.

### 3.2 Communication Pattern: The "Async Wait" (Human-in-the-loop Style)

Since LangGraph nodes are Python functions, we cannot simple `await` for hours. We use the **"Interrupt & Resume"** pattern supported by LangGraph's checkpointer.
*   **Interrupt & Resume**: Graph execution is paused while waiting for external events.
*   **Single-Listener Pattern**: Nodes listen **only** to task results (`worker:developer:output`). System health events (`worker:lifecycle`) are handled by `worker-manager`.

> **Crash Handling Architecture:**
> 
> LangGraph does NOT listen to `worker:lifecycle`. Instead:
> 1. `worker-manager` is the **sole consumer** of `worker:lifecycle`
> 2. When worker crashes (OOM, Docker fail, timeout), `worker-manager` detects this via lifecycle event
> 3. `worker-manager` publishes a **failure result** to `worker:developer:output`:
>    ```python
>    DeveloperWorkerOutput(
>        status="failed",
>        error="Worker crashed: OOM killed",
>        task_id=...,
>        request_id=...
>    )
>    ```
> 4. LangGraph receives this as a normal result and handles retry/failure logic

1.  **Node Execution**: The node (e.g., `Developer`) prepares a payload.
2.  **Publish**: It publishes the payload to Redis (`worker:commands`) with a `thread_id`.
3.  **Interrupt**: The node returns a special `Command(interrupt=True)` or simply ends its turn, expecting an input from a specific key. **The execution suspends and state is saved to Postgres.** The Python process is freed.
4.  **External Work**: The Worker/Service processes the task (taking minutes/hours).
5.  **Resume (The Listener)**:
    *   **Worker Results (Redis)**: A `ResponseListener` component listens to `worker:developer:output`. It correlates events by `task_id` and resumes the graph.
    *   **Service Results**: For services like Scaffolder, listen to `scaffolder:results`.
    *   **Action**: Call `graph.update_state(...)` and `graph.invoke(...)` to resume.

### 3.3 Error Handling in LangGraph

When LangGraph receives a `DeveloperWorkerOutput` with `status="failed"`:

```python
# Inside DeveloperNode or RouterNode
def handle_worker_result(state: GraphState, result: DeveloperWorkerOutput):
    if result.status == "failed":
        # Check retry policy
        if state.retry_count < MAX_RETRIES and is_retryable(result.error):
            return Command(
                goto="create_developer_worker",
                update={"retry_count": state.retry_count + 1}
            )
        else:
            # Fatal failure - notify user, mark task failed
            return Command(
                goto="fail_task",
                update={"error": result.error}
            )
    # ... handle success
```

**Retryable errors:**
- `"Worker crashed: OOM killed"` → Retry with resource adjustment
- `"Worker timeout"` → Retry with extended timeout
- `"Docker unavailable"` → Not retryable (infrastructure issue)

## 4. Concurrency & Scalability

This architecture is **Event-Driven and Stateless** (in terms of memory).

*   **Concurrency**: 
    *   The service is NOT blocked while waiting. 1 instance can handle thousands of "active" (but waiting) flows because they are just rows in Postgres.
    *   Active processing (deciding next step) takes milliseconds.
*   **Scaling**:
    *   Since state is in Postgres (shared) and events are in Redis (shared), you can run **N copies** of the `langgraph` service.
    *   Any instance can pick up the "Resume" event from Redis and advance the graph.
    *   **No Sticky Sessions required.**

## 5. Dependencies

**Allowed:**
*   `langgraph`, `langchain-core`
*   `redis` (async)
*   `asyncpg` (for specialized queries / state persistence)
*   `pydantic`
*   `structlog`

**BANNED:**
*   `ansible`, `terraform`
*   `git` CLI
*   `docker` CLI or SDK
*   Heavy ML libraries (numpy, pandas) - moved to Analyzers/Workers if needed.

## 6. API (Redis Interfaces)

### 5.1 Triggers (Inputs)

The service acts as a Consumer Group listening to high-level intent queues:

*   `engineering:queue` -> Spawns/Resumes `EngineeringGraph`
*   `deploy:queue` -> Spawns/Resumes `DevOpsGraph`

### 5.2 Emitted Commands (Outputs)

Nodes within the graphs primarily publish to:

*   `worker:commands` (Target: `worker-manager`)
*   `ansible:deploy:queue` (Target: `infra-service`)
