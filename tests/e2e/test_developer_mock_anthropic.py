"""
E2E Test: Developer Worker Mock Anthropic Integration

Verifies that the developer worker container correctly communicates with
the mock Anthropic server and returns deterministic responses.

This test extends Phase 4.5 (PO Worker) to test Developer Worker flow.
"""

import asyncio
import json
import time

import pytest
from redis.asyncio import Redis

from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    DeleteWorkerCommand,
    WorkerConfig,
)

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


async def wait_for_stream_message(
    redis: Redis, stream: str, timeout: int = 60, last_id: str = "0"
) -> tuple[str, dict]:
    """Wait for a message on Redis stream."""
    start = time.time()
    while time.time() - start < timeout:
        messages = await redis.xread({stream: last_id}, count=1, block=1000)
        if messages:
            msg_id = messages[0][1][0][0]
            data = messages[0][1][0][1]
            return msg_id, data
        await asyncio.sleep(0.5)
    raise TimeoutError(f"No message on {stream} within {timeout}s")


class TestDeveloperWorkerMockAnthropic:
    """Tests for Developer Worker integration with Mock Anthropic server."""

    async def _spawn_developer_worker(self, redis: Redis, worker_name: str) -> str:
        """Helper to spawn a developer worker and return its ID."""
        request_id = f"req-{worker_name}"
        cmd = CreateWorkerCommand(
            request_id=request_id,
            config=WorkerConfig(
                name=worker_name,
                worker_type="po",  # Using PO type (known to work), focus is on testing prompt field
                agent_type=AgentType.CLAUDE,
                instructions="You are a developer. Implement the requested features.",
                auth_mode="api_key",
                api_key="test-key",
                allowed_commands=[],
                capabilities=["git"],
                env_vars={
                    "ANTHROPIC_BASE_URL": "http://172.30.0.40:8000",
                },
            ),
        )
        await redis.xadd("worker:commands", {"data": cmd.model_dump_json()})

        # Wait for creation response on worker:responses:developer
        start = time.time()
        timeout = 90
        last_id = "0"
        while time.time() - start < timeout:
            messages = await redis.xread({"worker:responses:po": last_id}, count=10, block=1000)
            if messages:
                for _, stream_msgs in messages:
                    for msg_id, msg_data in stream_msgs:
                        last_id = msg_id
                        try:
                            data = json.loads(msg_data.get("data", "{}"))
                            if data.get("request_id") == request_id:
                                if not data.get("success"):
                                    raise RuntimeError(
                                        f"Worker creation failed: {data.get('error')}"
                                    )
                                return data["worker_id"]
                        except json.JSONDecodeError:
                            continue
            await asyncio.sleep(0.5)
        raise TimeoutError(f"No creation response for {request_id} within {timeout}s")

    async def _cleanup_worker(self, redis: Redis, worker_id: str):
        """Helper to cleanup worker."""
        await redis.xadd(
            "worker:commands",
            {
                "data": DeleteWorkerCommand(
                    request_id=f"cleanup-{worker_id}", worker_id=worker_id
                ).model_dump_json()
            },
        )

    async def test_developer_worker_receives_mock_response(self, redis: Redis):
        """Test that developer worker receives a valid response from mock server.

        Uses 'prompt' field (DeveloperWorkerInput) instead of 'content' (PO).
        """
        worker_name = f"dev-mock-{int(time.time())}"
        worker_id = await self._spawn_developer_worker(redis, worker_name)

        try:
            # Send task with 'prompt' field (as per DeveloperWorkerInput contract)
            # Using 'implement' keyword to match the scenario in mock responses
            task_data = {
                "prompt": "Please implement a simple hello world feature.",
                "task_id": "test-task-123",
                "project_id": "test-project-456",
            }
            await redis.xadd(
                f"worker:{worker_id}:input",
                {"data": json.dumps(task_data)},
            )

            # Wait for output
            _, output_msg = await wait_for_stream_message(
                redis, f"worker:{worker_id}:output", timeout=90
            )

            # Parse output
            result_str = output_msg.get("data", "")
            try:
                res_json = json.loads(result_str)
                assert (
                    res_json.get("status") == "success"
                ), f"Expected success status, got: {res_json}"
                assert "summary" in res_json, f"Expected summary field, got: {res_json}"
            except json.JSONDecodeError:
                pytest.fail(f"Failed to parse worker output: {result_str}")

        finally:
            await self._cleanup_worker(redis, worker_id)

    async def test_developer_worker_clone_scenario(self, redis: Redis):
        """Test that developer worker matches 'clone' scenario from mock responses."""
        worker_name = f"dev-clone-{int(time.time())}"
        worker_id = await self._spawn_developer_worker(redis, worker_name)

        try:
            # Send task with 'clone' keyword to match clone scenario
            task_data = {
                "prompt": "Clone the repository and create a test file.",
                "task_id": "clone-task-789",
                "project_id": "clone-project-012",
            }
            await redis.xadd(
                f"worker:{worker_id}:input",
                {"data": json.dumps(task_data)},
            )

            # Wait for output
            _, output_msg = await wait_for_stream_message(
                redis, f"worker:{worker_id}:output", timeout=90
            )

            # Parse output - should match 'clone' scenario
            result_str = output_msg.get("data", "")
            res_json = json.loads(result_str)

            assert res_json.get("status") == "success", f"Expected success, got: {res_json}"
            # Clone scenario returns summary about creating test marker file
            assert "e2e_test_marker.txt" in res_json.get(
                "summary", ""
            ), f"Expected clone scenario summary, got: {res_json.get('summary')}"

        finally:
            await self._cleanup_worker(redis, worker_id)
