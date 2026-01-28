"""
E2E Test: Worker Mock Anthropic Integration

Verifies that the worker container correctly communicates with the mock Anthropic server
and returns deterministic responses based on our scenarios.

Uses conftest.py fixtures which handle:
- Redis connection
- Worker base image building in DIND
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


class TestWorkerMockAnthropic:
    """Tests for Worker integration with Mock Anthropic server."""

    async def _spawn_worker(self, redis: Redis, worker_name: str) -> str:
        """Helper to spawn a worker and return its ID."""
        request_id = f"req-{worker_name}"
        cmd = CreateWorkerCommand(
            request_id=request_id,
            config=WorkerConfig(
                name=worker_name,
                worker_type="po",  # Use PO worker - responses go to worker:responses:po
                agent_type=AgentType.CLAUDE,
                instructions="You are an assistant. Complete the requested task.",
                auth_mode="api_key",  # Use API key mode for testing (no host session needed)
                api_key="test-key",  # Mock server doesn't validate keys
                allowed_commands=[],
                capabilities=[],
                # Point worker to mock-anthropic server
                env_vars={
                    "ANTHROPIC_BASE_URL": "http://172.30.0.40:8000",
                },
            ),
        )
        await redis.xadd("worker:commands", {"data": cmd.model_dump_json()})

        # Wait for creation response on worker:responses:po (based on worker_type)
        # We need to filter by request_id since multiple responses may be on this stream
        start = time.time()
        timeout = 60
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

    async def test_worker_receives_mock_response(self, redis: Redis):
        """Test that worker receives a valid response from mock server."""
        worker_name = f"mock-test-{int(time.time())}"
        worker_id = await self._spawn_worker(redis, worker_name)

        try:
            # Send generic prompt - will match "default" scenario
            prompt = "Hello, this is a test."
            await redis.xadd(
                f"worker:{worker_id}:input",
                {"data": json.dumps({"content": prompt})},
            )

            # Wait for output
            _, output_msg = await wait_for_stream_message(
                redis, f"worker:{worker_id}:output", timeout=60
            )

            # Parse output - worker-wrapper returns parsed JSON from <result> tags
            result_str = output_msg.get("data", "")
            try:
                res_json = json.loads(result_str)
                # The result should be the parsed JSON from mock-anthropic's <result> tags
                # Check for expected structure
                assert (
                    res_json.get("status") == "success"
                ), f"Expected success status, got: {res_json}"
                assert "summary" in res_json, f"Expected summary field, got: {res_json}"
            except json.JSONDecodeError:
                pytest.fail(f"Failed to parse worker output: {result_str}")

        finally:
            await self._cleanup_worker(redis, worker_id)

    async def test_worker_response_matches_scenario(self, redis: Redis):
        """Test that worker receives scenario-specific response."""
        worker_name = f"scenario-test-{int(time.time())}"
        worker_id = await self._spawn_worker(redis, worker_name)

        try:
            # Send prompt with "implement" keyword to match implement scenario
            prompt = "Please implement the feature."
            await redis.xadd(
                f"worker:{worker_id}:input",
                {"data": json.dumps({"content": prompt})},
            )

            # Wait for output
            _, output_msg = await wait_for_stream_message(
                redis, f"worker:{worker_id}:output", timeout=60
            )

            # Parse output - worker-wrapper returns parsed JSON from <result> tags
            result_str = output_msg.get("data", "")
            res_json = json.loads(result_str)

            # Should match 'implement' scenario from responses.py
            # returns: {"status": "success", "summary": "Implementation completed successfully"}
            assert res_json.get("status") == "success", f"Expected success status, got: {res_json}"
            assert (
                res_json.get("summary") == "Implementation completed successfully"
            ), f"Expected 'Implementation completed successfully', got: {res_json.get('summary')}"

        finally:
            await self._cleanup_worker(redis, worker_id)
