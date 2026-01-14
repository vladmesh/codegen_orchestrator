import uuid

import pytest
from tenacity import retry, stop_after_delay, wait_fixed

from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    WorkerConfig,
)

TEST_TIMEOUT = 60


@pytest.mark.integration
async def test_factory_cli_installed(redis_client, docker_client):
    """Factory worker must have factory CLI installed."""
    request_id = str(uuid.uuid4())
    worker_id = f"test-factory-{request_id[:8]}"

    config = WorkerConfig(
        name=worker_id,
        worker_type="developer",
        agent_type=AgentType.FACTORY,
        instructions="Test",
        allowed_commands=["*"],
        capabilities=[],
        auth_mode="api_key",
        api_key="sk-test-factory-key",
    )

    cmd = CreateWorkerCommand(request_id=request_id, config=config)
    await redis_client.xadd("worker:commands", {"data": cmd.model_dump_json()})

    @retry(stop=stop_after_delay(TEST_TIMEOUT), wait=wait_fixed(1))
    async def wait_for_container():
        try:
            container = docker_client.containers.get(f"worker-{worker_id}")
            if container.status != "running":
                raise Exception("Container not running")
            return container
        except Exception:
            raise Exception("Container not found") from None

    container = await wait_for_container()

    # Check factory CLI
    exit_code, output = container.exec_run("which droid")
    assert exit_code == 0, f"droid not found: {output.decode()}"

    # Check env var
    exit_code, output = container.exec_run("env")
    assert exit_code == 0
    assert "FACTORY_API_KEY=sk-test-factory-key" in output.decode()
    assert "ANTHROPIC_API_KEY" not in output.decode()

    # Check instructions path (Factory agent uses AGENTS.md)
    exit_code, output = container.exec_run("cat /workspace/AGENTS.md")
    assert exit_code == 0, f"AGENTS.md not found: {output.decode()}"
    assert "Test" in output.decode()
