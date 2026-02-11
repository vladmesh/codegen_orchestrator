# Reuse helper from test_worker_execution.py
# In a real scenario I might refactor this into a conftest or shared utility,
# but for now I'll duplicate the simple wait function to keep the test standalone.
import time
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    CreateWorkerResponse,
    DeleteWorkerCommand,
    WorkerCapability,
    WorkerConfig,
)


async def wait_for_stream_message(
    redis: Redis, stream: str, timeout: int = 30, last_id: str = "0"
) -> dict:
    start = time.time()
    current_id = last_id
    while time.time() - start < timeout:
        messages = await redis.xread({stream: current_id}, count=1, block=1000)
        if messages:
            msg_id = messages[0][1][0][0]
            fields = messages[0][1][0][1]
            result = {
                k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
                for k, v in fields.items()
            }
            result["_msg_id"] = msg_id
            return result
    raise TimeoutError(f"No message received on {stream} within {timeout}s")


async def cleanup_worker(redis: Redis, worker_id: str):
    """Send delete command for worker."""
    cmd = DeleteWorkerCommand(request_id=f"cleanup-{worker_id}", worker_id=worker_id)
    await redis.xadd(REDIS_STREAM_COMMANDS, {"data": cmd.model_dump_json()})


REDIS_STREAM_COMMANDS = "worker:commands"
REDIS_STREAM_DEV_RESPONSES = "worker:responses:developer"


@pytest.mark.integration
@pytest.mark.asyncio
class TestTaskInjection:
    async def test_task_injection_location(self, redis_client, docker_client):
        """
        Verify that TASK.md is injected into /home/worker/TASK.md
        and NOT /workspace/TASK.md
        """
        req_id = f"test-req-{uuid4().hex[:6]}"
        task_content = "This is a test task content."

        command = CreateWorkerCommand(
            request_id=req_id,
            config=WorkerConfig(
                name=f"test-task-{req_id}",
                worker_type="developer",
                agent_type=AgentType.CLAUDE,
                instructions="You are a test assistant.",
                task_content=task_content,
                allowed_commands=["project.get"],
                capabilities=[WorkerCapability.GIT],
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": command.model_dump_json()})

        # Wait for response
        response = await wait_for_stream_message(
            redis_client, REDIS_STREAM_DEV_RESPONSES, timeout=120
        )
        data_str = response.get("data")
        result = CreateWorkerResponse.model_validate_json(data_str)

        assert result.success is True, f"Worker creation failed: {result.error}"
        worker_id = result.worker_id

        try:
            container = docker_client.containers.get(f"worker-{worker_id}")

            # Check /home/worker/TASK.md exists and has content
            exit_code, output = container.exec_run("cat /home/worker/TASK.md")
            assert exit_code == 0, "TASK.md not found in /home/worker/"
            assert task_content.encode() == output, f"Unexpected content: {output}"

            # Check /workspace/TASK.md does NOT exist
            exit_code, output = container.exec_run("ls /workspace/TASK.md")
            assert exit_code != 0, "TASK.md SHOULD NOT be in /workspace/"

        finally:
            await cleanup_worker(redis_client, worker_id)
