# Testing Strategy: LangGraph Service

This document defines the testing strategy for the `langgraph` service ("The Orchestrator").

## 1. Philosophy: Component Testing

We use a **Black Box / Component Testing** approach.
The service is treated as a complete system that consumes inputs (Redis messages) and produces outputs (Redis commands/State updates).

*   **Real Components**:
    *   **LangGraph Service**: Running as a subprocess or Docker container.
    *   **Redis**: Real instance (isolated/flushdb).
    *   **Postgres**: Real instance (pgvector, for Checkpointing).

*   **Mocked Components**:
    *   **The World**: A `TestHarness` that simulates all other services (scaffolder, workers, infra, API).

### Why this approach?
1.  **Verifies Persistence**: We confirm that "Interrupt & Resume" actually saves/loads state from Postgres.
2.  **Verifies Contracts**: We ensure the service emits exact DTOs expected by consumers.
3.  **Speed**: No need to spin up real LLMs or build Docker images for workers.

## 2. Test Infrastructure: The `TestHarness`

The Harness is an async context manager that subscribes to all "Outgoing" queues and publishes to "Incoming" queues.

```python
class TestHarness:
    async def __aenter__(self):
        # 1. Flush Redis & Postgres
        # 2. Start LangGraph Service (subprocess)
        # 3. Subscribe to:
        #    - scaffolder:queue
        #    - worker:commands
        #    - ansible:deploy:queue
        #    - deploy:result:*
        pass

    async def send_engineering_request(self, project_id: str, task: str):
        """Publishes to engineering:queue"""
        pass

    async def expect_scaffold_request(self) -> ScaffolderMessage:
        """Waits for message in scaffolder:queue"""
        pass
    
    async def complete_scaffolding_simulation(self, project_id: str):
        """
        Simulates Scaffolder success:
        1. Updates 'Mock DB' (or simple Redis key if we mock API)
        2. (If LangGraph polls) updates status so LG sees it.
        """
        pass

    async def expect_worker_creation(self) -> CreateWorkerCommand:
        """Waits for worker:commands (create)"""
        pass
    
    async def send_worker_started(self, worker_id: str):
        """Publishes to worker:responses (started)"""
        pass

    # ... other helper methods
```

## 3. Test Scenarios

### 3.1 The "Engineering Flow" (Happy Path)

This scenario verifies the chain: `Engineering Start` -> `Scaffolder` -> `Developer` -> `Success`.

**Steps:**

1.  **Trigger**: Harness sends `EngineeringMessage(project_id="p1", task="Init Backend")`.
    *   *Check*: LangGraph acknowledges message (XACK).

2.  **Scaffolding Phase**:
    *   *Assert*: Harness receives `ScaffolderMessage` in `scaffolder:queue`.
    *   *Assert Payload*: `project_id="p1"`, `modules=[BACKEND]`.
    *   *Action*: Harness simulates scaffolding completion (Update DB status to `SCAFFOLDED`).
    *   *Wait*: Allow LangGraph to poll/resume.

3.  **Developer Isolation (Worker Creation)**:
    *   *Assert*: Harness receives `CreateWorkerCommand` in `worker:commands`.
    *   *Assert Payload*: `type="CLAUDE"`, `worker_type="developer"`.
    *   *Action*: Harness sends `CreateWorkerResponse(success=True, worker_id="w1")` to `worker:responses`.

4.  **Developer Execution (Task Delegation)**:
    *   *Assert*: Harness receives `DeveloperWorkerInput` in `worker:developer:input`.
    *   *Assert Payload*: `task_id` matches, `project_id="p1"`, `prompt` contains "Init Backend".
    *   *Action*: Harness sends `DeveloperWorkerOutput` to `worker:developer:output`:
        *   `commit_sha="abc1234"`
        *   `verdict={status="success"}`

5.  **Completion**:
    *   *Assert*: Mock API receives `PATCH /api/tasks/{task_id}` with `status="completed"`.
    *   *Verification*: Check Postgres state -> Workflow "DONE".

### 3.2 The "Deploy Flow" (Happy Path)

This scenario verifies: `Deploy Start` -> `Env Analysis` -> `Secrets` -> `Ansible` -> `Success`.

**Steps:**

1.  **Trigger**: Harness sends `DeployMessage(project_id="p1", environment="prod")`.

2.  **DevOps Subgraph Execution**:
    *   (Internal nodes like `EnvAnalyzer` might be skipped if they are pure logic, or we verify logs).
    *   *Assert*: Harness receives `AnsibleDeployMessage` in `ansible:deploy:queue`.
    *   *Assert Payload*: `server_ip`, `repo_full_name`, `secrets_ref`.

3.  **Ansible Execution**:
    *   *Action*: Harness sends `AnsibleDeployResult(status="success", url="http://deploy.com")` to `deploy:result:{request_id}`.

4.  **Completion**:
    *   *Assert*: LangGraph updates Task status to `COMPLETED` (via API mock or DB).

### 3.3 Interrupt & Resume (Persistence Check)

This scenario explicitly kills the service mid-flight to ensure state recovery.

**Steps:**

1.  **Trigger**: Start Engineering Flow.
2.  **Wait for Scaffolding**: Reach the step where LangGraph waits for Scaffolder.
3.  **KILL**: Terminate the `langgraph` subprocess.
4.  **Simulate Delay**: Wait 2 seconds.
5.  **Simulate Result**: Harness simulates scaffolding completion (DB Update).
6.  **RESTART**: Start `langgraph` subprocess again.
7.  **Assert Recovery**:
    *   LangGraph should *not* restart from the beginning (should not send `ScaffolderMessage` again).
    *   LangGraph should detect the completion (poll or event) and proceed to **Worker Creation**.
    *   *Assert*: Harness receives `CreateWorkerCommand`.

### 3.4 Error Handling & Retries

**Steps:**

1.  **Trigger**: Engineering Flow.
2.  **Worker Failure**:
    *   Harness sends `WorkerLifecycleEvent(event="failed")` or `WorkerResponse(status="failed")`.
3.  **Retry Logic**:
    *   *Assert*: LangGraph attempts to create worker again (if retry policy exists) OR fails the task.
    *   *Assert*: Mock API receives `PATCH /api/tasks/{task_id}` with `status="failed"`.

## 4. Mocking Strategy

*   **API Calls**: Since LangGraph might call `api` service (to update Task status), we need to:
    *   Option A: Run a lightweight Mock API (FastAPI) alongside Redis.
    *   Option B: Mock `httpx` inside the `langgraph` process (requires injecting a mock client via env var or test config).
    *   **Decision**: **Option A (Mock Server)**. It's more robust for "Black Box" testing. The Harness spins up a dummy uvicorn server on `localhost:8001` that responds to `/projects/{id}` and `/tasks`.

## 5. Next Steps for Implementation

1.  Create `tests/integration/test_langgraph_flow.py`.
2.  Implement `TestHarness` fixture (Redis + Postgres + MockAPI).
3.  Implement `test_engineering_happy_path`.
4.  Implement `test_persistence_recovery`.
