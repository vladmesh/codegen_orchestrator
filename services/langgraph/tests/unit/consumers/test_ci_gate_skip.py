"""Unit tests for CI gate skip logic — ordinary story tasks skip CI gate.

Tests:
- Ordinary story tasks (created_by=architect) skip CI gate
- CI check tasks (created_by=system) run CI gate
- Standalone tasks (no planning_task_id) run CI gate
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.redis = AsyncMock()
    r.redis.xadd = AsyncMock()
    r.publish_flat = AsyncMock()
    return r


def _result(commit_sha="abc123", worker_id=None):
    return {
        "engineering_status": "done",
        "commit_sha": commit_sha,
        "worker_id": worker_id,
    }


def _project():
    return {"id": "proj-1", "name": "test-project", "config": {"modules": ["backend"]}}


class TestShouldRunCIGate:
    """Tests for _should_run_ci_gate helper."""

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.api_client")
    async def test_standalone_task_returns_true(self, mock_api):
        from src.consumers.engineering import _should_run_ci_gate

        result = await _should_run_ci_gate(None)
        assert result is True
        mock_api.get.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.api_client")
    async def test_ci_check_task_returns_true(self, mock_api):
        from src.consumers.engineering import _should_run_ci_gate

        mock_api.get = AsyncMock(return_value={"created_by": "system"})
        result = await _should_run_ci_gate("task-ci-1")
        assert result is True

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.api_client")
    async def test_architect_task_returns_false(self, mock_api):
        from src.consumers.engineering import _should_run_ci_gate

        mock_api.get = AsyncMock(return_value={"created_by": "architect"})
        result = await _should_run_ci_gate("task-arch-1")
        assert result is False

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.api_client")
    async def test_api_failure_returns_false(self, mock_api):
        from src.consumers.engineering import _should_run_ci_gate

        mock_api.get = AsyncMock(side_effect=Exception("API down"))
        result = await _should_run_ci_gate("task-123")
        assert result is False


class TestCIGateSkipInHandleSuccess:
    @pytest.mark.asyncio
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers.engineering._run_ci_gate_and_handle_failure", new_callable=AsyncMock)
    @patch("src.consumers.engineering._should_run_ci_gate", new_callable=AsyncMock)
    @patch("src.consumers.engineering.api_client")
    async def test_ordinary_story_task_skips_ci_gate(
        self, mock_api, mock_should, mock_ci_run, mock_publish, mock_redis
    ):
        """Tasks created by architect should skip CI gate entirely."""
        from src.consumers.engineering import _handle_engineering_success

        mock_should.return_value = False
        mock_api.patch = AsyncMock()
        mock_api.post = AsyncMock()

        result = await _handle_engineering_success(
            result=_result(),
            task_id="eng-1",
            project=_project(),
            callback_stream="po:response:abc",
            redis=mock_redis,
            skip_deploy=True,
            developer_started_at=datetime.now(UTC),
            user_id="u1",
            planning_task_id="task-arch-1",
            story_id="story-1",
        )

        assert result["status"] == "success"
        mock_ci_run.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers.engineering._run_ci_gate_and_handle_failure", new_callable=AsyncMock)
    @patch("src.consumers.engineering._should_run_ci_gate", new_callable=AsyncMock)
    @patch("src.consumers.engineering.api_client")
    async def test_ci_check_task_runs_ci_gate(
        self, mock_api, mock_should, mock_ci_run, mock_publish, mock_redis
    ):
        """CI check tasks (created_by=system) should run CI gate."""
        from src.consumers.engineering import _handle_engineering_success

        mock_should.return_value = True
        mock_ci_run.return_value = None  # CI passed
        mock_api.patch = AsyncMock()
        mock_api.post = AsyncMock()

        result = await _handle_engineering_success(
            result=_result(),
            task_id="eng-1",
            project=_project(),
            callback_stream="po:response:abc",
            redis=mock_redis,
            skip_deploy=True,
            developer_started_at=datetime.now(UTC),
            user_id="u1",
            planning_task_id="task-ci-1",
            story_id="story-1",
        )

        assert result["status"] == "success"
        mock_ci_run.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers.engineering._run_ci_gate_and_handle_failure", new_callable=AsyncMock)
    @patch("src.consumers.engineering._should_run_ci_gate", new_callable=AsyncMock)
    @patch("src.consumers.engineering.api_client")
    async def test_standalone_task_runs_ci_gate(
        self, mock_api, mock_should, mock_ci_run, mock_publish, mock_redis
    ):
        """Standalone tasks (no planning_task_id) should always run CI gate."""
        from src.consumers.engineering import _handle_engineering_success

        mock_should.return_value = True
        mock_ci_run.return_value = None  # CI passed
        mock_api.patch = AsyncMock()
        mock_api.post = AsyncMock()

        result = await _handle_engineering_success(
            result=_result(),
            task_id="eng-1",
            project=_project(),
            callback_stream="po:response:abc",
            redis=mock_redis,
            skip_deploy=False,
            developer_started_at=datetime.now(UTC),
            user_id="u1",
            planning_task_id=None,
            story_id=None,
        )

        assert result["status"] == "success"
        mock_ci_run.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.set_story_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers.engineering._run_ci_gate_and_handle_failure", new_callable=AsyncMock)
    @patch("src.consumers.engineering._should_run_ci_gate", new_callable=AsyncMock)
    @patch("src.consumers.engineering.api_client")
    async def test_skipped_ci_gate_registers_worker_for_reuse(
        self, mock_api, mock_should, mock_ci_run, mock_publish, mock_set_worker, mock_redis
    ):
        """When CI gate is skipped, worker should still be registered for story reuse."""
        from src.consumers.engineering import _handle_engineering_success

        mock_should.return_value = False
        mock_api.patch = AsyncMock()
        mock_api.post = AsyncMock()

        await _handle_engineering_success(
            result=_result(worker_id="worker-abc"),
            task_id="eng-1",
            project=_project(),
            callback_stream=None,
            redis=mock_redis,
            skip_deploy=True,
            developer_started_at=datetime.now(UTC),
            user_id="u1",
            planning_task_id="task-arch-1",
            story_id="story-1",
        )

        mock_set_worker.assert_called_once_with(mock_redis.redis, "story-1", "worker-abc")
        mock_ci_run.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers.engineering._run_ci_gate_and_handle_failure", new_callable=AsyncMock)
    @patch("src.consumers.engineering._should_run_ci_gate", new_callable=AsyncMock)
    @patch("src.consumers.engineering.api_client")
    async def test_ci_gate_failure_returns_failure_dict(
        self, mock_api, mock_should, mock_ci_run, mock_publish, mock_redis
    ):
        """When CI gate returns a failure, _handle_engineering_success propagates it."""
        from src.consumers.engineering import _handle_engineering_success

        mock_should.return_value = True
        mock_ci_run.return_value = {"status": "failed", "error": "CI failed"}
        mock_api.patch = AsyncMock()
        mock_api.post = AsyncMock()

        result = await _handle_engineering_success(
            result=_result(),
            task_id="eng-1",
            project=_project(),
            callback_stream="po:response:abc",
            redis=mock_redis,
            skip_deploy=True,
            developer_started_at=datetime.now(UTC),
            user_id="u1",
            planning_task_id="task-ci-1",
            story_id="story-1",
        )

        assert result["status"] == "failed"
