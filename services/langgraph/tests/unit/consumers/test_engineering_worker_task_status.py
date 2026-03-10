"""Tests for engineering worker task status updates.

When a run has a linked planning_task_id, the engineering worker should:
1. Update task status alongside run status
2. Write iteration_end event on completion
3. Skip deploy (deploy is triggered by dispatcher on story complete)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def mock_redis():
    """Mock RedisStreamClient."""
    r = AsyncMock()
    r.redis = AsyncMock()
    r.redis.xadd = AsyncMock()
    r.publish_flat = AsyncMock()
    return r


@pytest.fixture
def mock_api():
    """Patch api_client methods used by the engineering worker."""
    with patch("src.consumers.engineering.api_client") as api:
        api.patch = AsyncMock()
        api.post = AsyncMock()
        api.get_project = AsyncMock(return_value={"id": "proj-1", "name": "test", "config": {}})
        api.get_primary_repository = AsyncMock(
            return_value={"git_url": "https://github.com/org/test"}
        )
        yield api


class TestTaskStatusUpdates:
    """When planning_task_id is present, worker updates task status."""

    @pytest.mark.asyncio
    @patch("src.consumers.engineering._wait_for_ci_and_fix", new_callable=AsyncMock)
    @patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock)
    async def test_updates_task_on_success(self, mock_delete, mock_ci_gate, mock_redis, mock_api):
        """On success with planning_task_id: task → done, event written."""
        mock_ci_gate.return_value = (True, [], False, None)

        from src.consumers.engineering import _handle_engineering_success

        result = {
            "engineering_status": "done",
            "commit_sha": "abc123",
            "worker_id": "w-1",
        }

        await _handle_engineering_success(
            result=result,
            task_id="eng-1",
            project={"id": "proj-1", "name": "test", "config": {}},
            callback_stream=None,
            redis=mock_redis,
            skip_deploy=False,
            user_id="u-1",
            action="feature",
            planning_task_id="task-42",
        )

        # Should transition task through in_ci → testing → done
        task_transition_calls = [
            c for c in mock_api.post.call_args_list if "tasks/task-42/transition" in str(c)
        ]
        assert len(task_transition_calls) == 3
        statuses = [c.kwargs["params"]["to_status"] for c in task_transition_calls]
        assert statuses == ["in_ci", "testing", "done"]

        # Should write iteration_end event
        event_calls = [c for c in mock_api.post.call_args_list if "tasks/task-42/events" in str(c)]
        assert len(event_calls) == 1
        event_data = event_calls[0][1]["json"]
        assert event_data["event_type"] == "iteration_end"
        assert event_data["details"]["commit_sha"] == "abc123"

    @pytest.mark.asyncio
    @patch("src.consumers.engineering._wait_for_ci_and_fix", new_callable=AsyncMock)
    @patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock)
    async def test_skips_deploy_when_task_linked(
        self, mock_delete, mock_ci_gate, mock_redis, mock_api
    ):
        """With planning_task_id, deploy is skipped (dispatcher handles it)."""
        mock_ci_gate.return_value = (True, [], False, None)

        from src.consumers.engineering import _handle_engineering_success

        result = {
            "engineering_status": "done",
            "commit_sha": "abc123",
            "worker_id": "w-1",
        }

        await _handle_engineering_success(
            result=result,
            task_id="eng-1",
            project={"id": "proj-1", "name": "test", "config": {}},
            callback_stream=None,
            redis=mock_redis,
            skip_deploy=False,
            user_id="u-1",
            action="feature",
            planning_task_id="task-42",
        )

        # Should NOT publish to deploy queue
        deploy_calls = [c for c in mock_redis.redis.xadd.call_args_list if "deploy" in str(c)]
        assert len(deploy_calls) == 0

    @pytest.mark.asyncio
    @patch("src.consumers.engineering._wait_for_ci_and_fix", new_callable=AsyncMock)
    @patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock)
    async def test_backward_compat_no_task_id(
        self, mock_delete, mock_ci_gate, mock_redis, mock_api
    ):
        """Without planning_task_id, old behavior: deploy triggers as before."""
        mock_ci_gate.return_value = (True, [], False, None)
        mock_api.post.return_value = AsyncMock(status_code=201)

        from src.consumers.engineering import _handle_engineering_success

        result = {
            "engineering_status": "done",
            "commit_sha": "abc123",
            "worker_id": "w-1",
        }

        await _handle_engineering_success(
            result=result,
            task_id="eng-1",
            project={"id": "proj-1", "name": "test", "config": {}},
            callback_stream=None,
            redis=mock_redis,
            skip_deploy=False,
            user_id="u-1",
            action="feature",
            # No planning_task_id
        )

        # Should publish to deploy queue (old behavior)
        deploy_calls = [c for c in mock_redis.redis.xadd.call_args_list if "deploy" in str(c)]
        assert len(deploy_calls) == 1

    @pytest.mark.asyncio
    async def test_task_failed_on_engineering_failure(self, mock_redis, mock_api):
        """When engineering fails with planning_task_id, task → failed."""
        from src.consumers.engineering import _update_task_status

        await _update_task_status(mock_api, "task-42", "failed")

        mock_api.post.assert_called_once()
        call_args = mock_api.post.call_args
        assert "tasks/task-42/transition" in call_args[0][0]
        assert call_args[1]["params"] == {"to_status": "failed"}
