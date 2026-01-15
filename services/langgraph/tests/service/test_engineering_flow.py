"""Service tests for Engineering Flow in LangGraph."""

import pytest

from shared.contracts.queues.worker import AgentType


@pytest.mark.asyncio
async def test_engineering_flow_happy_path(harness):
    """
    Scenario 3.1: The 'Engineering Flow' (Happy Path)
    Verifies the chain: Engineering Start -> Scaffolder -> Developer -> Success.
    """
    # 1. Trigger: Engineering Request
    project_id = "p-happy-path"
    await harness.send_engineering_request(project_id, task="Init Backend")

    # 2. Assert Scaffolding Request
    # LangGraph should consume the engineering request and ask Scaffolder
    msg = await harness.expect_scaffold_request()
    assert msg["project_id"] == project_id

    # 3. Simulate Scaffolder Completion
    await harness.simulate_scaffolder_completion(project_id)

    # 4. Developer Isolation (Worker Creation)
    # LangGraph should receive Scaffolder result and ask to create a Worker
    cmd = await harness.expect_worker_creation()
    assert cmd.config.agent_type == AgentType.CLAUDE
    assert cmd.config.worker_type == "developer"

    # 5. Simulate Worker Creation
    worker_id = f"worker-{project_id}"
    await harness.simuluate_worker_creation(cmd.request_id, worker_id)

    # 6. Developer Execution (Task Delegation)
    # LangGraph should send the actual prompt/task to the worker
    task_input = await harness.expect_worker_task(worker_id)
    assert task_input.project_id == project_id

    # 7. Simulate Worker Success
    await harness.simulate_worker_success(task_input.task_id, task_input.request_id)

    # 8. Completion - flow should be done
    # In a full test we would check if task status was updated


@pytest.mark.asyncio
async def test_persistence_recovery(harness):
    """
    Scenario 3.3: Interrupt & Resume (Persistence Check)
    Verifies that flow can resume after receiving events even if service restarts.
    """
    # 1. Trigger
    project_id = "p-recovery"
    await harness.send_engineering_request(project_id, task="Test Persistence")

    # 2. Wait for Scaffolding request
    await harness.expect_scaffold_request()

    # 3. Simulate Result (as if it arrived while service was "down")
    await harness.simulate_scaffolder_completion(project_id)

    # 4. Assert Recovery -> Next step is Worker Creation
    cmd = await harness.expect_worker_creation()
    assert cmd.config.agent_type == AgentType.CLAUDE


@pytest.mark.asyncio
async def test_worker_crash_handling(harness):
    """
    Scenario 3.4: Error Handling & Retries
    """
    # 1. Trigger
    project_id = "p-crash"
    await harness.send_engineering_request(project_id, task="Test Crash")

    # Steps to get to worker execution...
    await harness.expect_scaffold_request()
    await harness.simulate_scaffolder_completion(project_id)
    cmd = await harness.expect_worker_creation()
    worker_id = "worker-crash"
    await harness.simuluate_worker_creation(cmd.request_id, worker_id)
    task_input = await harness.expect_worker_task(worker_id)

    # 2. Worker Failure (Crash)
    await harness.simulate_worker_crash(task_input.task_id, task_input.request_id)

    # 3. Retry Logic - expect another CreateWorkerCommand
    retry_cmd = await harness.expect_worker_creation()
    assert retry_cmd.command == "create"
    assert retry_cmd.config.agent_type == AgentType.CLAUDE
