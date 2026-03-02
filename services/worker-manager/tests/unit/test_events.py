"""Tests for DockerEventsListener container death detection."""

import json

import pytest
from unittest.mock import AsyncMock

from src.events import DockerEventsListener, WORKER_DEAD_STATUS


class TestHandleEvent:
    """Tests for _handle_event — the core logic, no Docker dependency."""

    @pytest.mark.asyncio
    async def test_publishes_error_to_worker_output_stream(self):
        """When a worker container dies, should publish error to worker:{id}:output."""
        mock_redis = AsyncMock()
        listener = DockerEventsListener(mock_redis)

        event = {
            "Type": "container",
            "Action": "die",
            "Actor": {
                "ID": "abc123",
                "Attributes": {
                    "name": "worker-dev-todo-api-3f2f114f",
                    "exitCode": "137",
                    "com.codegen.worker.id": "dev-todo-api-3f2f114f",
                    "com.codegen.type": "worker",
                },
            },
        }

        await listener._handle_event(event)

        # Should publish to the worker's output stream
        mock_redis.xadd.assert_called_once()
        call_args = mock_redis.xadd.call_args
        stream = call_args[0][0]
        data = call_args[0][1]

        assert stream == "worker:dev-todo-api-3f2f114f:output"
        payload = json.loads(data["data"])
        assert payload["status"] == "failed"
        assert "137" in payload["error"]
        assert payload["worker_id"] == "dev-todo-api-3f2f114f"

    @pytest.mark.asyncio
    async def test_marks_worker_status_as_dead(self):
        """Should set worker:status:{id} to DEAD for liveness check fallback."""
        mock_redis = AsyncMock()
        listener = DockerEventsListener(mock_redis)

        event = {
            "Type": "container",
            "Action": "die",
            "Actor": {
                "Attributes": {
                    "name": "worker-dev-abc",
                    "exitCode": "1",
                    "com.codegen.worker.id": "dev-abc",
                    "com.codegen.type": "worker",
                },
            },
        }

        await listener._handle_event(event)

        mock_redis.hset.assert_called_once_with("worker:status:dev-abc", mapping={"status": WORKER_DEAD_STATUS})

    @pytest.mark.asyncio
    async def test_ignores_normal_exit(self):
        """Exit code 0 means worker finished normally — should not publish error."""
        mock_redis = AsyncMock()
        listener = DockerEventsListener(mock_redis)

        event = {
            "Type": "container",
            "Action": "die",
            "Actor": {
                "Attributes": {
                    "name": "worker-dev-ok",
                    "exitCode": "0",
                    "com.codegen.worker.id": "dev-ok",
                    "com.codegen.type": "worker",
                },
            },
        }

        await listener._handle_event(event)

        mock_redis.xadd.assert_not_called()
        mock_redis.hset.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_non_worker_containers(self):
        """Containers without com.codegen.worker.id label should be ignored."""
        mock_redis = AsyncMock()
        listener = DockerEventsListener(mock_redis)

        event = {
            "Type": "container",
            "Action": "die",
            "Actor": {
                "Attributes": {
                    "name": "postgres_db",
                    "exitCode": "1",
                },
            },
        }

        await listener._handle_event(event)
        mock_redis.xadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_redis_publish_error_gracefully(self):
        """If Redis publish fails, should not crash — just log."""
        mock_redis = AsyncMock()
        mock_redis.xadd.side_effect = Exception("Redis connection lost")
        listener = DockerEventsListener(mock_redis)

        event = {
            "Type": "container",
            "Action": "die",
            "Actor": {
                "Attributes": {
                    "exitCode": "137",
                    "com.codegen.worker.id": "dev-broken",
                    "com.codegen.type": "worker",
                },
            },
        }

        # Should not raise
        await listener._handle_event(event)

        # hset should still be attempted
        mock_redis.hset.assert_called_once()
