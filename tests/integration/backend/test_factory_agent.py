import uuid

import pytest
from tenacity import retry, stop_after_delay, wait_fixed

from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
)

from .conftest import scaffolded_worker_config, wait_for_worker_ready

TEST_TIMEOUT = 60


@pytest.mark.integration
async def test_factory_cli_installed(
    redis_client, docker_client, scaffolded_workspace, worker_authority
):
    """Factory worker must have factory CLI installed."""
    request_id = str(uuid.uuid4())
    worker_id = f"test-factory-{request_id[:8]}"

    config = scaffolded_worker_config(
        scaffolded_workspace,
        name=worker_id,
        worker_type="developer",
        agent_type=AgentType.FACTORY,
        instructions="Test",
        allowed_commands=["*"],
        capabilities=[],
        ownership=await worker_authority(),
        auth_mode="api_key",
        api_key="sk-test-factory-key",
    )

    cmd = CreateWorkerCommand(request_id=request_id, config=config)
    await redis_client.xadd("worker:commands", {"data": cmd.model_dump_json()})

    container = await wait_for_worker_ready(
        redis_client,
        docker_client,
        request_id=request_id,
        worker_id=worker_id,
        create_timeout=TEST_TIMEOUT,
        readiness_timeout=TEST_TIMEOUT,
    )

    # Check factory CLI
    exit_code, output = container.exec_run("which droid")
    assert exit_code == 0, f"droid not found: {output.decode()}"

    # Check env var
    exit_code, output = container.exec_run("env")
    assert exit_code == 0
    assert "FACTORY_API_KEY=sk-test-factory-key" in output.decode()
    assert "ANTHROPIC_API_KEY" not in output.decode()

    # Check instructions path: the orchestrator's own file, not the product's AGENTS.md.
    # It is written after the container starts — retry
    @retry(stop=stop_after_delay(TEST_TIMEOUT), wait=wait_fixed(1))
    async def wait_for_instructions():
        ec, out = container.exec_run("cat /workspace/WORKER_INSTRUCTIONS.md")
        assert ec == 0, f"WORKER_INSTRUCTIONS.md not found: {out.decode()}"
        return out

    output = await wait_for_instructions()
    assert "Test" in output.decode()
