import json
from uuid import uuid4

import pytest

from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    WorkerCapability,
    WorkerChannels,
    WorkerConfig,
)
from shared.contracts.queues.worker_result import WorkerResultStatus

from .conftest import (
    REDIS_STREAM_COMMANDS,
    REDIS_STREAM_DEV_RESPONSES,
    wait_for_create_response,
    wait_for_stream_message,
)


@pytest.mark.integration
@pytest.mark.asyncio
class TestWorkerExecution:
    @pytest.mark.asyncio
    async def test_create_claude_worker_with_git_capability(
        self, redis_client, docker_client, scaffolded_workspace
    ):
        """
        Scenario D.1: Create Claude worker with GIT capability.
        """
        req_id = f"test-req-{uuid4().hex[:6]}"
        command = CreateWorkerCommand(
            request_id=req_id,
            config=WorkerConfig(
                name="test-claude",
                worker_type="developer",
                agent_type=AgentType.CLAUDE,
                instructions="You are a test assistant.",
                allowed_commands=["project.get"],
                capabilities=[WorkerCapability.GIT],
                repo_id=scaffolded_workspace,
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": command.model_dump_json()})

        result = await wait_for_create_response(
            redis_client, REDIS_STREAM_DEV_RESPONSES, request_id=req_id
        )

        assert result.success is True, f"Worker creation failed: {result.error}"
        assert result.worker_id is not None
        worker_id = result.worker_id

        # Verify container configuration
        container = docker_client.containers.get(f"worker-{worker_id}")

        # Check git is installed
        exit_code, output = container.exec_run("git --version")
        assert exit_code == 0
        assert b"git version" in output

        # Check CLAUDE.md exists with instructions
        exit_code, output = container.exec_run("cat /workspace/CLAUDE.md")
        assert exit_code == 0
        assert b"test assistant" in output

    @pytest.mark.asyncio
    async def test_create_factory_worker_with_curl_capability(
        self, redis_client, docker_client, scaffolded_workspace
    ):
        """
        Scenario D.2: Create Factory worker with CURL capability.
        """
        req_id = f"test-req-{uuid4().hex[:6]}"
        command = CreateWorkerCommand(
            request_id=req_id,
            config=WorkerConfig(
                name="test-factory",
                worker_type="developer",
                agent_type=AgentType.FACTORY,
                instructions="You are a Factory assistant.",
                allowed_commands=["project.get"],
                capabilities=[WorkerCapability.CURL],
                repo_id=scaffolded_workspace,
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": command.model_dump_json()})

        result = await wait_for_create_response(
            redis_client, REDIS_STREAM_DEV_RESPONSES, request_id=req_id
        )

        assert result.success is True, f"Worker creation failed: {result.error}"
        worker_id = result.worker_id

        container = docker_client.containers.get(f"worker-{worker_id}")

        # Check AGENTS.md (not CLAUDE.md)
        exit_code, _ = container.exec_run("cat /workspace/AGENTS.md")
        assert exit_code == 0

        exit_code, _ = container.exec_run("ls /workspace/CLAUDE.md")
        assert exit_code != 0  # Should NOT exist

        # Check curl installed
        exit_code, _ = container.exec_run("curl --version")
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_different_agent_types_produce_different_images(
        self, redis_client, docker_client, scaffolded_workspace
    ):
        """
        Scenario B: Image caching respects agent_type.
        """
        from .conftest import _create_scaffolded_workspace

        # Create Claude worker
        req_id_1 = f"cache-1-{uuid4().hex[:6]}"
        cmd1 = CreateWorkerCommand(
            request_id=req_id_1,
            config=WorkerConfig(
                name="cache-claude",
                worker_type="developer",
                agent_type=AgentType.CLAUDE,
                instructions="test",
                allowed_commands=[],
                capabilities=[WorkerCapability.GIT],
                repo_id=scaffolded_workspace,
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": cmd1.model_dump_json()})
        result1 = await wait_for_create_response(
            redis_client, REDIS_STREAM_DEV_RESPONSES, request_id=req_id_1
        )
        assert result1.success, f"Worker 1 creation failed: {result1.error}"
        worker1_id = result1.worker_id
        assert worker1_id == "cache-claude", f"Unexpected worker1_id: {worker1_id}"

        # Create Factory worker with same capabilities (needs its own workspace)
        factory_repo_id = _create_scaffolded_workspace()
        req_id_2 = f"cache-2-{uuid4().hex[:6]}"
        cmd2 = CreateWorkerCommand(
            request_id=req_id_2,
            config=WorkerConfig(
                name="cache-factory",
                worker_type="developer",
                agent_type=AgentType.FACTORY,
                instructions="test",
                allowed_commands=[],
                capabilities=[WorkerCapability.GIT],
                repo_id=factory_repo_id,
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": cmd2.model_dump_json()})
        result2 = await wait_for_create_response(
            redis_client, REDIS_STREAM_DEV_RESPONSES, request_id=req_id_2
        )
        assert result2.success, f"Worker 2 creation failed: {result2.error}"
        worker2_id = result2.worker_id
        assert worker2_id == "cache-factory", f"Unexpected worker2_id: {worker2_id}"
        assert worker1_id != worker2_id, "Worker IDs must be different"

        # Get container images
        container1 = docker_client.containers.get(f"worker-{worker1_id}")
        container2 = docker_client.containers.get(f"worker-{worker2_id}")

        # Image TAGS should be DIFFERENT (different agent_type affects hash)
        tags1 = container1.image.tags
        tags2 = container2.image.tags

        # Extract the worker:hash tag
        worker_tag1 = [t for t in tags1 if t.startswith("worker:")]
        worker_tag2 = [t for t in tags2 if t.startswith("worker:")]

        assert worker_tag1, f"No worker tag found for container1: {tags1}"
        assert worker_tag2, f"No worker tag found for container2: {tags2}"
        assert worker_tag1[0] != worker_tag2[0], (
            f"Tags should differ: {worker_tag1[0]} vs {worker_tag2[0]}"
        )

    @pytest.mark.asyncio
    async def test_worker_executes_task_with_mocked_claude(
        self, redis_client, docker_client, scaffolded_workspace
    ):
        """
        Scenario: Worker receives task via Redis input stream and writes result to output.
        Actually verifies connectivity. Since we don't mock LLM yet, we expect execution attempt.
        """
        req_id = f"exec-test-{uuid4().hex[:6]}"

        # 1. Create Worker (Factory Agent for simplicity or Claude)
        command = CreateWorkerCommand(
            request_id=req_id,
            config=WorkerConfig(
                name="exec-worker",
                worker_type="developer",
                agent_type=AgentType.FACTORY,
                instructions="Echo test agent",
                allowed_commands=["project.get"],
                capabilities=[WorkerCapability.CURL],
                repo_id=scaffolded_workspace,
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": command.model_dump_json()})

        # 2. Get Worker ID
        result = await wait_for_create_response(
            redis_client, REDIS_STREAM_DEV_RESPONSES, request_id=req_id
        )
        assert result.success, f"Worker creation failed: {result.error}"
        worker_id = result.worker_id

        # 3. Verify Streams logic (via internal knowledge of channels)
        input_stream = WorkerChannels.INPUT_PATTERN.value.format(worker_id=worker_id)

        # 4. Send Input
        # Factory runner expects 'content' in data
        task_data = {"content": "Hello World"}
        await redis_client.xadd(input_stream, {"data": json.dumps(task_data)})

        # 5. Wait for the typed worker result on the output stream.
        # The worker publishes a WorkerResult (completed/failed/blocked/rejected) — even a
        # failed execution writes a terminal result here. This is the worker output
        # contract; the old `worker:lifecycle` stream was removed.
        output_stream = WorkerChannels.OUTPUT_PATTERN.value.format(worker_id=worker_id)
        output_msg = await wait_for_stream_message(redis_client, output_stream, timeout=60)
        output_data = json.loads(output_msg["data"])
        assert output_data["status"] in {s.value for s in WorkerResultStatus}
