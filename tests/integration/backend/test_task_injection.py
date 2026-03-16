from uuid import uuid4

import pytest

from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    DeleteWorkerCommand,
    WorkerCapability,
    WorkerConfig,
)

from .conftest import (
    REDIS_STREAM_COMMANDS,
    REDIS_STREAM_DEV_RESPONSES,
    wait_for_create_response,
)


async def cleanup_worker(redis_client, worker_id: str | None):
    """Send delete command for worker (no-op if worker_id is None)."""
    if not worker_id:
        return
    cmd = DeleteWorkerCommand(request_id=f"cleanup-{worker_id}", worker_id=worker_id)
    await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": cmd.model_dump_json()})


@pytest.mark.integration
@pytest.mark.asyncio
class TestTaskInjection:
    async def test_task_injection_location(self, redis_client, docker_client):
        """
        Verify that TASK.md is injected into /workspace/TASK.md.
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

        # Use wait_for_create_response which filters by request_id
        # (wait_for_stream_message picks up stale delete responses from cleanup)
        result = await wait_for_create_response(
            redis_client, REDIS_STREAM_DEV_RESPONSES, req_id, timeout=120
        )

        assert result.success is True, f"Worker creation failed: {result.error}"
        worker_id = result.worker_id

        try:
            container = docker_client.containers.get(f"worker-{worker_id}")

            # Check /workspace/TASK.md exists and has content
            exit_code, output = container.exec_run("cat /workspace/TASK.md")
            assert exit_code == 0, "TASK.md not found in /workspace/"
            assert task_content.encode() == output, f"Unexpected content: {output}"

        finally:
            await cleanup_worker(redis_client, result.worker_id)

    async def test_env_hints_in_task_md(self, redis_client, docker_client):
        """Verify that env_hints content appears in TASK.md inside the worker."""
        # Build task content inline — the formatting logic is tested in langgraph unit tests.
        # Here we only verify that the worker receives and mounts the content correctly.
        task_content = (
            "# Task: Build hints-test\n\n"
            "## Provided Environment Variables\n\n"
            "The Product Owner has already defined the following environment variables "
            "for this project.\n"
            "You MUST use them in your code via `os.getenv()` or `pydantic-settings`. "
            "Do NOT ask the user for them.\n\n"
            "- `ADMIN_TELEGRAM_ID`: Telegram ID of the bot admin\n"
            "- `OPENAI_API_KEY`: OpenAI key for responses\n"
        )

        req_id = f"test-hints-{uuid4().hex[:6]}"
        command = CreateWorkerCommand(
            request_id=req_id,
            config=WorkerConfig(
                name=f"test-hints-{req_id}",
                worker_type="developer",
                agent_type=AgentType.CLAUDE,
                instructions="You are a test assistant.",
                task_content=task_content,
                allowed_commands=["project.get"],
                capabilities=[WorkerCapability.GIT],
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": command.model_dump_json()})

        # Use wait_for_create_response which filters by request_id
        result = await wait_for_create_response(
            redis_client, REDIS_STREAM_DEV_RESPONSES, req_id, timeout=120
        )

        assert result.success is True, f"Worker creation failed: {result.error}"
        worker_id = result.worker_id

        try:
            container = docker_client.containers.get(f"worker-{worker_id}")

            exit_code, output = container.exec_run("cat /workspace/TASK.md")
            assert exit_code == 0, "TASK.md not found in /workspace/"
            task_text = output.decode()

            assert "Provided Environment Variables" in task_text
            assert "ADMIN_TELEGRAM_ID" in task_text
            assert "Telegram ID of the bot admin" in task_text
            assert "OPENAI_API_KEY" in task_text
            assert "os.getenv()" in task_text

        finally:
            await cleanup_worker(redis_client, result.worker_id)
