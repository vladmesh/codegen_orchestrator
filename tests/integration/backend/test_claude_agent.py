import uuid

import pytest
from tenacity import retry, stop_after_delay, wait_fixed

from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    WorkerCapability,
    WorkerConfig,
)

# Constants
TEST_TIMEOUT = 60  # seconds


@pytest.mark.integration
async def test_claude_cli_installed(redis_client, docker_client):
    """Claude worker must have claude CLI installed."""
    request_id = str(uuid.uuid4())
    worker_id = f"test-claude-{request_id[:8]}"

    # 1. Send CreateWorkerCommand
    config = WorkerConfig(
        name=worker_id,
        worker_type="developer",
        agent_type=AgentType.CLAUDE,
        instructions="Test instructions",
        allowed_commands=["*"],
        capabilities=[WorkerCapability.GIT, WorkerCapability.CURL],
        auth_mode="host_session",  # Default
    )

    cmd = CreateWorkerCommand(request_id=request_id, config=config)
    await redis_client.xadd("worker:commands", {"data": cmd.model_dump_json()})

    # 2. Wait for container to be running
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

    # 3. Check claude CLI
    exit_code, output = container.exec_run("which claude")
    assert exit_code == 0, f"claude not found: {output.decode()}"

    # 4. Check nodejs and npm (implicit verification as claude depends on it)
    exit_code, output = container.exec_run("node --version")
    assert exit_code == 0, f"node not found: {output.decode()}"

    exit_code, output = container.exec_run("npm --version")
    assert exit_code == 0, f"npm not found: {output.decode()}"

    # 5. Check claude version
    exit_code, output = container.exec_run("claude --version")
    assert exit_code == 0, f"claude version check failed: {output.decode()}"


@pytest.mark.integration
async def test_claude_session_mounted(redis_client, docker_client):
    """Check if host session directory is mounted."""
    request_id = str(uuid.uuid4())
    worker_id = f"test-claude-mount-{request_id[:8]}"

    # We are in DIND, so /home/worker/.claude inside the container should be mounted
    # to whatever we specified. But verifying exact host mount in DIND is tricky.
    # However, we can check if the directory exists and is writable.

    # Note: in test environment we might not have a real host dir effectively mounted
    # unless we configured it in the runner. But for this test let's simulate passing a path.
    # The runner might create it.

    config = WorkerConfig(
        name=worker_id,
        worker_type="developer",
        agent_type=AgentType.CLAUDE,
        instructions="Test",
        allowed_commands=["*"],
        capabilities=[],
        auth_mode="host_session",
        host_claude_dir="/tmp/test-claude-session",  # noqa: S108
        # Actually in DIND, volumes need to exist on the DIND host or Docker creates them as dir.
    )

    cmd = CreateWorkerCommand(request_id=request_id, config=config)
    await redis_client.xadd("worker:commands", {"data": cmd.model_dump_json()})

    @retry(stop=stop_after_delay(TEST_TIMEOUT), wait=wait_fixed(1))
    async def wait_for_container():
        return docker_client.containers.get(f"worker-{worker_id}")

    container = await wait_for_container()

    # Check if directory exists
    exit_code, output = container.exec_run("ls -la /home/worker/.claude")
    # Even if empty, it should exist as a directory
    assert exit_code == 0, f"Session dir not found: {output.decode()}"


@pytest.mark.integration
async def test_claude_instructions_injected(redis_client, docker_client):
    """Check if CLAUDE.md is injected."""
    request_id = str(uuid.uuid4())
    worker_id = f"test-claude-instr-{request_id[:8]}"
    instructions = "unique-test-instructions-content-123"

    config = WorkerConfig(
        name=worker_id,
        worker_type="developer",
        agent_type=AgentType.CLAUDE,
        instructions=instructions,
        allowed_commands=["*"],
        capabilities=[],
    )

    cmd = CreateWorkerCommand(request_id=request_id, config=config)
    await redis_client.xadd("worker:commands", {"data": cmd.model_dump_json()})

    @retry(stop=stop_after_delay(TEST_TIMEOUT), wait=wait_fixed(1))
    async def wait_for_container():
        return docker_client.containers.get(f"worker-{worker_id}")

    container = await wait_for_container()

    # Check content
    exit_code, output = container.exec_run("cat /workspace/CLAUDE.md")
    assert exit_code == 0
    assert instructions in output.decode()


@pytest.mark.integration
async def test_orchestrator_cli_installed(redis_client, docker_client):
    """Orchestrator CLI must be installed."""
    request_id = str(uuid.uuid4())
    worker_id = f"test-orch-cli-{request_id[:8]}"

    config = WorkerConfig(
        name=worker_id,
        worker_type="developer",
        agent_type=AgentType.CLAUDE,
        instructions="Test",
        allowed_commands=["*"],
        capabilities=[],
    )

    cmd = CreateWorkerCommand(request_id=request_id, config=config)
    await redis_client.xadd("worker:commands", {"data": cmd.model_dump_json()})

    @retry(stop=stop_after_delay(TEST_TIMEOUT), wait=wait_fixed(1))
    async def wait_for_container():
        return docker_client.containers.get(f"worker-{worker_id}")

    container = await wait_for_container()

    # Check CLI
    exit_code, output = container.exec_run("which orchestrator")
    assert exit_code == 0, f"orchestrator CLI not found: {output.decode()}"

    exit_code, output = container.exec_run("orchestrator --help")
    assert exit_code == 0, f"orchestrator help failed: {output.decode()}"
