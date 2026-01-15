"""Test fixtures and harness for LangGraph service tests."""

import asyncio
import json
import os
import uuid

import pytest
from redis.asyncio import Redis

from shared.contracts.queues.developer_worker import (
    DeveloperWorkerInput,
    DeveloperWorkerOutput,
)
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.queues.scaffolder import ScaffolderResult
from shared.contracts.queues.worker import (
    CreateWorkerCommand,
    CreateWorkerResponse,
)

# Mock the API URL for tests
os.environ.setdefault("API_URL", "http://localhost:8001")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")


class TestHarness:
    """
    Test Harness for LangGraph Service Testing.
    Simulates the environment (Redis, Other Services) around the LangGraph service.
    """

    def __init__(self, redis: Redis):
        self.redis = redis
        self.captured_commands: list[dict] = []
        self._last_read_ids: dict[str, str] = {}

    async def setup(self):
        """Clean slate before test."""
        await self.redis.flushdb()
        self._last_read_ids.clear()

    async def send_engineering_request(self, project_id: str, task: str) -> str:
        """Publishes to engineering:queue to trigger the flow."""
        task_id = f"task-{uuid.uuid4()}"
        message = EngineeringMessage(
            task_id=task_id,
            project_id=project_id,
            user_id=123,
        )
        await self.redis.xadd("engineering:queue", {"data": message.model_dump_json()})
        return task_id

    async def send_deploy_request(self, project_id: str, env: str = "prod") -> str:
        """Publishes to deploy:queue."""
        from shared.contracts.queues.deploy import DeployMessage

        task_id = f"task-{uuid.uuid4()}"
        message = DeployMessage(
            task_id=task_id,
            project_id=project_id,
            user_id=123,
        )
        await self.redis.xadd("deploy:queue", {"data": message.model_dump_json()})
        return task_id

    async def expect_scaffold_request(self, timeout: int = 10) -> dict:
        """Waits for message in scaffolder:queue."""
        msg = await self._wait_for_message("scaffolder:queue", timeout)
        data = json.loads(msg.get("data", "{}"))
        return data

    async def simulate_scaffolder_completion(self, project_id: str):
        """Simulates Scaffolder success by publishing result."""
        result = ScaffolderResult(
            request_id=f"req-{uuid.uuid4()}",
            status="success",
            project_id=project_id,
            repo_url=f"https://github.com/test-org/{project_id}",
            files_generated=10,
        )
        await self.redis.xadd("scaffolder:results", {"data": result.model_dump_json()})

    async def expect_worker_creation(self, timeout: int = 10) -> CreateWorkerCommand:
        """Waits for worker:commands (create)."""
        msg_data = await self._wait_for_message("worker:commands", timeout)
        return CreateWorkerCommand.model_validate_json(msg_data["data"])

    async def simuluate_worker_creation(self, request_id: str, worker_id: str = "worker-1"):
        """Sends CreateWorkerResponse to worker:responses:developer."""
        response = CreateWorkerResponse(
            request_id=request_id,
            success=True,
            worker_id=worker_id,
        )
        await self.redis.xadd("worker:responses:developer", {"data": response.model_dump_json()})

    async def expect_worker_task(self, worker_id: str, timeout: int = 10) -> DeveloperWorkerInput:
        """Waits for task in worker:developer:input."""
        msg_data = await self._wait_for_message("worker:developer:input", timeout)
        return DeveloperWorkerInput.model_validate_json(msg_data["data"])

    async def simulate_worker_success(self, task_id: str, request_id: str):
        """Simulates Worker completing the task successfully."""
        output = DeveloperWorkerOutput(
            request_id=request_id,
            status="success",
            task_id=task_id,
            commit_sha="abc1234",
        )
        await self.redis.xadd("worker:developer:output", {"data": output.model_dump_json()})

    async def simulate_worker_crash(self, task_id: str, request_id: str):
        """Simulates Worker crashing (OOM)."""
        output = DeveloperWorkerOutput(
            request_id=request_id,
            status="failed",
            error="Worker crashed: OOM killed",
            task_id=task_id,
        )
        await self.redis.xadd("worker:developer:output", {"data": output.model_dump_json()})

    async def _wait_for_message(self, stream: str, timeout: int) -> dict:
        """Wait for a NEW message on Redis stream."""
        # Use last read ID to only get new messages
        last_id = self._last_read_ids.get(stream, "0")

        start_time = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start_time) < timeout:
            messages = await self.redis.xread({stream: last_id}, count=1, block=1000)

            if messages:
                # messages format: [[stream_name, [[id, fields], ...]], ...]
                stream_name, stream_messages = messages[0]
                msg_id, fields = stream_messages[0]

                # Store last ID for this stream
                msg_id_str = msg_id.decode() if isinstance(msg_id, bytes) else msg_id
                self._last_read_ids[stream] = msg_id_str

                # Decode fields if bytes
                decoded_fields = {
                    (k.decode() if isinstance(k, bytes) else k): (
                        v.decode() if isinstance(v, bytes) else v
                    )
                    for k, v in fields.items()
                }
                return decoded_fields

        raise TimeoutError(f"No message received on {stream} within {timeout}s")


@pytest.fixture
async def redis_client():
    """Provides a dedicated Redis client for tests."""
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    client = Redis.from_url(redis_url)
    yield client
    await client.aclose()


@pytest.fixture
async def harness(redis_client):
    """Provides the Test Harness."""
    harness = TestHarness(redis_client)
    await harness.setup()
    return harness
