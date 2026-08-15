import uuid
from uuid import uuid4

import pytest

from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    WorkerCapability,
    WorkerConfig,
    WorkerOwnership,
)

from .conftest import delete_test_worker, exec_in_running_worker, wait_for_worker_ready

# Constants
CREATE_TIMEOUT = 240  # seconds, includes a cold worker image build inside DinD
READINESS_TIMEOUT = 30  # seconds, once worker-manager has completed creation


def _ownership() -> WorkerOwnership:
    """A distinct owner per worker; these tests are not about who.

    Distinct on purpose: two workers of one project serialize on that project's
    workspace lock, so every worker here is made for its own project and run.
    """
    token = uuid4().hex[:8]
    return WorkerOwnership(
        project_id=f"proj-{token}", run_id=f"run-{token}", attempt_id=f"attempt-run-{token}"
    )


@pytest.mark.integration
async def test_claude_cli_installed(redis_client, docker_client, scaffolded_workspace):
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
        ownership=_ownership(),
        # This test verifies the binary, not host-session persistence. The DinD fixture has no
        # real subscription session, so keep the wrapper alive with its isolated test key.
        auth_mode="api_key",
        api_key="sk-ant-test-claude-key",
        repo_id=scaffolded_workspace,
    )

    cmd = CreateWorkerCommand(request_id=request_id, config=config)
    await redis_client.xadd("worker:commands", {"data": cmd.model_dump_json()})

    try:
        container = await wait_for_worker_ready(
            redis_client,
            docker_client,
            request_id=request_id,
            worker_id=worker_id,
            create_timeout=CREATE_TIMEOUT,
            readiness_timeout=READINESS_TIMEOUT,
        )

        exit_code, output = exec_in_running_worker(container, worker_id, "which claude")
        assert exit_code == 0, f"claude not found: {output.decode()}"

        exit_code, output = exec_in_running_worker(container, worker_id, "claude --version")
        assert exit_code == 0, f"claude version check failed: {output.decode()}"
    finally:
        await delete_test_worker(redis_client, docker_client, worker_id)


@pytest.mark.integration
async def test_claude_session_mounted(redis_client, docker_client, scaffolded_workspace):
    """Check if host session directory is mounted."""
    request_id = str(uuid.uuid4())
    worker_id = f"test-claude-mount-{request_id[:8]}"

    config = WorkerConfig(
        name=worker_id,
        worker_type="developer",
        agent_type=AgentType.CLAUDE,
        instructions="Test",
        allowed_commands=["*"],
        capabilities=[],
        ownership=_ownership(),
        auth_mode="host_session",
        host_claude_dir="/host-claude",
        repo_id=scaffolded_workspace,
    )

    cmd = CreateWorkerCommand(request_id=request_id, config=config)
    await redis_client.xadd("worker:commands", {"data": cmd.model_dump_json()})

    try:
        container = await wait_for_worker_ready(
            redis_client,
            docker_client,
            request_id=request_id,
            worker_id=worker_id,
            create_timeout=CREATE_TIMEOUT,
            readiness_timeout=READINESS_TIMEOUT,
        )

        probe = f"/home/worker/.claude/.mount-probe-{request_id}"
        exit_code, output = exec_in_running_worker(
            container,
            worker_id,
            (
                "sh -ec 'test -d /home/worker/.claude "
                f"&& touch {probe} && test -w {probe} && rm {probe}'"
            ),
        )
        assert exit_code == 0, f"Session directory is not mounted and writable: {output.decode()}"
    finally:
        await delete_test_worker(redis_client, docker_client, worker_id)


@pytest.mark.integration
async def test_claude_instructions_injected(redis_client, docker_client, scaffolded_workspace):
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
        ownership=_ownership(),
        # CLAUDE.md injection does not exercise persisted Claude authentication.
        # The DinD fixture intentionally has no real user session, so selecting
        # host_session here makes the wrapper reject its own required mount and
        # tests the wrong boundary.
        auth_mode="api_key",
        api_key="sk-ant-test-claude-key",
        repo_id=scaffolded_workspace,
    )

    cmd = CreateWorkerCommand(request_id=request_id, config=config)
    await redis_client.xadd("worker:commands", {"data": cmd.model_dump_json()})

    try:
        container = await wait_for_worker_ready(
            redis_client,
            docker_client,
            request_id=request_id,
            worker_id=worker_id,
            create_timeout=CREATE_TIMEOUT,
            readiness_timeout=READINESS_TIMEOUT,
        )

        ec, output = exec_in_running_worker(container, worker_id, "cat /workspace/CLAUDE.md")
        assert ec == 0, f"CLAUDE.md not found: {output.decode()}"
        assert instructions in output.decode()
    finally:
        await delete_test_worker(redis_client, docker_client, worker_id)


@pytest.mark.integration
async def test_stopped_instruction_worker_reports_startup_evidence(
    redis_client, docker_client, scaffolded_workspace
):
    """The prior dead-container path reports its exit rather than a Docker 409."""
    request_id = str(uuid.uuid4())
    worker_id = f"test-claude-stopped-{request_id[:8]}"
    config = WorkerConfig(
        name=worker_id,
        worker_type="developer",
        agent_type=AgentType.CLAUDE,
        instructions="This worker must not reach a Claude turn.",
        allowed_commands=["*"],
        capabilities=[],
        ownership=_ownership(),
        auth_mode="host_session",
        # The test-owned source stays root-owned, so the non-root worker wrapper
        # must reject it at startup after worker-manager injects instructions.
        # This follows the failed gate's ordering without exposing daemon files.
        host_claude_dir="/host-claude-unwritable",
        repo_id=scaffolded_workspace,
    )

    await redis_client.xadd(
        "worker:commands",
        {"data": CreateWorkerCommand(request_id=request_id, config=config).model_dump_json()},
    )

    try:
        with pytest.raises(AssertionError) as exc:
            await wait_for_worker_ready(
                redis_client,
                docker_client,
                request_id=request_id,
                worker_id=worker_id,
                create_timeout=CREATE_TIMEOUT,
                readiness_timeout=READINESS_TIMEOUT,
            )

        failure = str(exc.value)
        assert "status=exited" in failure
        assert "exit_code=1" in failure
        assert "not writable" in failure
    finally:
        await delete_test_worker(redis_client, docker_client, worker_id)
