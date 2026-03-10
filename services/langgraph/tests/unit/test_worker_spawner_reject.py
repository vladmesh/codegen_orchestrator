"""Unit tests for reject_reason propagation in SpawnResult and worker_spawner."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.clients.worker_spawner import SpawnResult


class TestSpawnResultRejectField:
    """SpawnResult should carry reject_reason field."""

    def test_reject_reason_default_none(self):
        result = SpawnResult(request_id="r1", success=False, exit_code=1, output="err")
        assert result.reject_reason is None

    def test_reject_reason_set(self):
        result = SpawnResult(
            request_id="r1",
            success=False,
            exit_code=1,
            output="rejected",
            reject_reason="Missing REGISTRY_PASSWORD secret",
        )
        assert result.reject_reason == "Missing REGISTRY_PASSWORD secret"
        assert result.success is False


class TestSendTaskRejectPropagation:
    """send_task_to_worker should populate reject_reason from worker output."""

    @pytest.mark.asyncio
    @patch("src.clients.worker_spawner.get_settings")
    @patch("src.clients.worker_spawner.redis")
    async def test_rejected_worker_output_populates_reject_reason(
        self, mock_redis_mod, mock_settings
    ):
        """When worker returns status=rejected, SpawnResult carries reject_reason."""
        mock_settings.return_value.redis_url = "redis://localhost:6379"

        mock_client = AsyncMock()
        mock_redis_mod.from_url.return_value = mock_client

        # Mock xgroup_create
        mock_client.xgroup_create = AsyncMock()

        # Mock xadd (sending task to worker)
        mock_client.xadd = AsyncMock()

        # Mock worker output: rejected
        worker_output = {
            "status": "rejected",
            "content": "Analysis complete",
            "reject_reason": "REGISTRY_PASSWORD secret is empty",
        }

        # _wait_for_response returns the worker output
        with patch(
            "src.clients.worker_spawner._wait_for_response",
            new_callable=AsyncMock,
            return_value=worker_output,
        ):
            from src.clients.worker_spawner import send_task_to_worker

            result = await send_task_to_worker(
                worker_id="dev-test-123",
                task_content="Fix CI",
                timeout_seconds=10,
            )

        assert result.success is False
        assert result.reject_reason == "REGISTRY_PASSWORD secret is empty"

    @pytest.mark.asyncio
    @patch("src.clients.worker_spawner.get_settings")
    @patch("src.clients.worker_spawner.redis")
    async def test_normal_success_no_reject_reason(self, mock_redis_mod, mock_settings):
        """Normal success output should have reject_reason=None."""
        mock_settings.return_value.redis_url = "redis://localhost:6379"

        mock_client = AsyncMock()
        mock_redis_mod.from_url.return_value = mock_client
        mock_client.xgroup_create = AsyncMock()
        mock_client.xadd = AsyncMock()

        worker_output = {
            "status": "success",
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
                worker_id="dev-test-123",
                task_content="Fix CI",
                timeout_seconds=10,
            )

        assert result.success is True
        assert result.reject_reason is None
