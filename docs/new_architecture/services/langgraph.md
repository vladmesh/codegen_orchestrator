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
*   **Two-Listener Pattern**: Nodes listen to BOTH task results (`worker:developer:output`) AND system health (`worker:lifecycle`) to handle crashes gracefully.

1.  **Node Execution**: The node (e.g., `Developer`) prepares a payload.
2.  **Publish**: It publishes the payload to Redis (`worker:commands`) with a `thread_id`.
3.  **Interrupt**: The node returns a special `Command(interrupt=True)` or simply ends its turn, expecting an input from a specific key. **The execution suspends and state is saved to Postgres.** The Python process is freed.
4.  **External Work**: The Worker/Service processes the task (taking minutes/hours).
5.  **Resume (The Listener)**:
    *   **Option A (Redis)**: A `ResponseListener` component (part of the runtime) listens to `worker:developer:output` AND `worker:lifecycle`. It correlates events by `task_id` or `worker_id` and resumes the graph.
    *   **Option B (Polling)**: For services like Scaffolder that only update DB, a `StatusPoller` checks for state transitions (e.g., `Project.status == SCAFFOLDED`).
    *   **Action**: Call `graph.update_state(...)` and `graph.invoke(...)` to resume.

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
