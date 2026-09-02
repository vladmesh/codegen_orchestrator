"""Unit tests for gave_up_reason propagation in SpawnResult and worker_spawner."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.queues.worker import WorkerOwnership
from src.clients.worker_spawner import SpawnResult

_OWNERSHIP = WorkerOwnership(project_id="proj-1", run_id="run-1", attempt_id="eng-attempt-1")


@pytest.fixture(autouse=True)
def _attempt_recording_stubbed():
    """These tests exercise the stream protocol, not the attempt bookkeeping.

    Naming the worker on its attempt is a real API write with its own test; here
    it would only be a live HTTP call in the middle of a Redis-level assertion.
    """
    with (
        patch("src.clients.worker_spawner.record_worker_on_attempt", new_callable=AsyncMock),
        patch("src.clients.worker_spawner.record_turn_on_attempt", new_callable=AsyncMock),
    ):
        yield


class TestSpawnResultGaveUpField:
    """SpawnResult should carry gave_up_reason field."""

    def test_gave_up_reason_default_none(self):
        result = SpawnResult(request_id="r1", success=False, exit_code=1, output="err")
        assert result.gave_up_reason is None

    def test_gave_up_reason_set(self):
        result = SpawnResult(
            request_id="r1",
            success=False,
            exit_code=1,
            output="rejected",
            gave_up_reason="Missing REGISTRY_PASSWORD secret",
        )
        assert result.gave_up_reason == "Missing REGISTRY_PASSWORD secret"
        assert result.success is False


class TestSendTaskGaveUpPropagation:
    """send_task_to_worker should populate gave_up_reason from worker output."""

    @pytest.mark.asyncio
    @patch("src.clients.worker_spawner.get_settings")
    @patch("src.clients.worker_spawner.redis")
    async def test_blocked_worker_output_populates_gave_up_reason(
        self, mock_redis_mod, mock_settings
    ):
        """When worker returns status=blocked, SpawnResult carries gave_up_reason."""
        mock_settings.return_value.redis_url = "redis://localhost:6379"

        mock_client = AsyncMock()
        mock_redis_mod.from_url.return_value = mock_client
        mock_client.xgroup_create = AsyncMock()
        mock_client.xadd = AsyncMock()

        worker_output = {
            "status": "blocked",
            "block_reason": "Missing API credentials for Stripe",
        }

        with patch(
            "src.clients.worker_spawner._wait_for_response",
            new_callable=AsyncMock,
            return_value=worker_output,
        ):
            from src.clients.worker_spawner import send_task_to_worker

            result = await send_task_to_worker(
                ownership=_OWNERSHIP,
                worker_id="dev-test-123",
                task_content="Fix CI",
                timeout_seconds=10,
            )

        assert result.success is False
        assert result.gave_up_reason == "Missing API credentials for Stripe"

    @pytest.mark.asyncio
    @patch("src.clients.worker_spawner.get_settings")
    @patch("src.clients.worker_spawner.redis")
    async def test_normal_success_no_gave_up_reason(self, mock_redis_mod, mock_settings):
        """Normal success output should have gave_up_reason=None."""
        mock_settings.return_value.redis_url = "redis://localhost:6379"

        mock_client = AsyncMock()
        mock_redis_mod.from_url.return_value = mock_client
        mock_client.xgroup_create = AsyncMock()
        mock_client.xadd = AsyncMock()

        worker_output = {
            "status": "completed",
            "content": "Fixed the issue",
            "commit_sha": "abc123",
        }

        with patch(
            "src.clients.worker_spawner._wait_for_response",
            new_callable=AsyncMock,
            return_value=worker_output,
        ):
            from src.clients.worker_spawner import send_task_to_worker

            result = await send_task_to_worker(
                ownership=_OWNERSHIP,
                worker_id="dev-test-123",
                task_content="Fix CI",
                timeout_seconds=10,
            )

        assert result.success is True
        assert result.gave_up_reason is None
