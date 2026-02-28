"""Service tests for Engineering Flow in LangGraph.

Note: These tests verify the LangGraph service flow using the TestHarness.
With the scaffolder removed, repo creation happens inline in the engineering
worker and scaffolding (copier + make setup) happens in worker-manager
via ScaffoldConfig. These tests focus on the worker creation/execution flow.
"""

import pytest

from shared.contracts.queues.worker import AgentType


@pytest.mark.asyncio
async def test_engineering_flow_happy_path(harness):
    """
    Scenario 3.1: The 'Engineering Flow' (Happy Path)
    Verifies the chain: Engineering Start -> Worker Creation -> Developer -> Success.
    """
    # 1. Trigger: Engineering Request
    project_id = "p-happy-path"
    await harness.send_engineering_request(project_id, task="Init Backend")

    # 2. Developer Isolation (Worker Creation)
    # Engineering worker creates repo inline, then developer node requests worker spawn
    cmd = await harness.expect_worker_creation()
    assert cmd.config.agent_type == AgentType.CLAUDE
    assert cmd.config.worker_type == "developer"

    # 3. Simulate Worker Creation
    worker_id = f"worker-{project_id}"
    await harness.simuluate_worker_creation(cmd.request_id, worker_id)

    # 4. Developer Execution (Task Delegation)
    # LangGraph should send the actual prompt/task to the worker
    task_input = await harness.expect_worker_task(worker_id)
    assert task_input.project_id == project_id

    # 5. Simulate Worker Success
    await harness.simulate_worker_success(task_input.task_id, task_input.request_id)

    # 6. Completion - flow should be done
    # In a full test we would check if task status was updated


@pytest.mark.asyncio
async def test_worker_crash_handling(harness):
    """
    Scenario 3.4: Error Handling & Retries
    """
    # 1. Trigger
    project_id = "p-crash"
    await harness.send_engineering_request(project_id, task="Test Crash")

    # 2. Get to worker execution
    cmd = await harness.expect_worker_creation()
    worker_id = "worker-crash"
    await harness.simuluate_worker_creation(cmd.request_id, worker_id)
    task_input = await harness.expect_worker_task(worker_id)

    # 3. Worker Failure (Crash)
    await harness.simulate_worker_crash(task_input.task_id, task_input.request_id)

    # 4. Retry Logic - expect another CreateWorkerCommand
    retry_cmd = await harness.expect_worker_creation()
    assert retry_cmd.command == "create"
    assert retry_cmd.config.agent_type == AgentType.CLAUDE
