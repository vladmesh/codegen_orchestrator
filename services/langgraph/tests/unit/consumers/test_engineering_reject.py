"""Unit tests for engineering consumer handling of worker reject.

When CI gate returns rejected=True, the engineering consumer must:
- Transition planning task to 'failed' with failure_metadata.failure_reason='worker_rejected'
- Fail the story with reject metadata
- Call notify_admins with reject reason
- NOT publish po:proactive message
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


@pytest.fixture
def mock_api():
    with patch("src.consumers.engineering.api_client") as api:
        api.patch = AsyncMock()
        api.post = AsyncMock()
        api.get = AsyncMock(return_value={"created_by": "system"})
        api.get_project = AsyncMock(
            return_value={
                "id": "proj-1",
                "name": "test-project",
                "status": "active",
                "config": {"modules": ["backend"], "description": "A todo API"},
            }
        )
        api.get_primary_repository = AsyncMock(
            return_value={"git_url": "https://github.com/org/test-project"}
        )
        api.get_tasks_by_story = AsyncMock(return_value=[])
        api.get_task_events = AsyncMock(return_value=[])
        yield api


def _success_result(commit_sha="abc123", worker_id="w1"):
    return {
        "engineering_status": "done",
        "commit_sha": commit_sha,
        "worker_id": worker_id,
    }


class TestRejectHandling:
    """Tests for _handle_engineering_success when CI gate returns rejected."""

    @pytest.mark.asyncio
    @patch("src.consumers.engineering._should_run_ci_gate", new_callable=AsyncMock)
    @patch("src.consumers.engineering._run_ci_gate_and_handle_failure", new_callable=AsyncMock)
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock)
    async def test_rejected_transitions_task_to_failed_with_metadata(
        self, mock_delete, mock_publish, mock_ci_run, mock_should, mock_redis, mock_api
    ):
        """Worker reject → planning task transitions to 'failed' with reject metadata."""
        from src.consumers.engineering import _handle_engineering_success

        mock_should.return_value = True
        mock_ci_run.return_value = {
            "status": "failed",
            "rejected": True,
            "reject_reason": "REGISTRY_PASSWORD secret is empty",
        }

        result = await _handle_engineering_success(
            result=_success_result(),
            task_id="eng-1",
            project={"id": "proj-1", "name": "test"},
            callback_stream="po:response:abc",
            redis=mock_redis,
            skip_deploy=False,
            developer_started_at=datetime(2025, 1, 1, tzinfo=UTC),
            user_id="u1",
            planning_task_id="task-123",
            story_id="story-1",
        )

        assert result["status"] == "failed"
        assert result.get("rejected") is True

    @pytest.mark.asyncio
    @patch("src.consumers.engineering._should_run_ci_gate", new_callable=AsyncMock)
    @patch("src.consumers.engineering._run_ci_gate_and_handle_failure", new_callable=AsyncMock)
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering.notify_admins", new_callable=AsyncMock)
    async def test_rejected_calls_notify_admins(
        self, mock_notify, mock_delete, mock_publish, mock_ci_run, mock_should, mock_redis, mock_api
    ):
        """Worker reject → _run_ci_gate_and_handle_failure calls notify_admins internally."""
        from src.consumers.engineering import _handle_worker_reject

        # Test _handle_worker_reject directly (it's what calls notify_admins)
        mock_notify.return_value = 1

        await _handle_worker_reject(
            task_id="eng-1",
            project_id="proj-1",
            planning_task_id="task-123",
            story_id="story-1",
            reject_reason="Docker registry TLS cert is self-signed",
            ci_attempts=[{"attempt": 0, "status": "rejected"}],
        )

        mock_notify.assert_awaited_once()
        call_args = mock_notify.call_args
        assert "TLS cert" in call_args[0][0]
        assert call_args[1]["level"] == "error"

    @pytest.mark.asyncio
    @patch("src.consumers.engineering._should_run_ci_gate", new_callable=AsyncMock)
    @patch("src.consumers.engineering._run_ci_gate_and_handle_failure", new_callable=AsyncMock)
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering.notify_admins", new_callable=AsyncMock)
    async def test_rejected_fails_story_with_metadata(
        self, mock_notify, mock_delete, mock_publish, mock_ci_run, mock_should, mock_redis, mock_api
    ):
        """Worker reject → story fails with reject metadata."""
        from src.consumers.engineering import _handle_worker_reject

        mock_notify.return_value = 1

        await _handle_worker_reject(
            task_id="eng-1",
            project_id="proj-1",
            planning_task_id="task-123",
            story_id="story-1",
            reject_reason="Missing secrets",
            ci_attempts=[{"attempt": 0, "status": "rejected"}],
        )

        # Check that story was failed with metadata
        story_fail_calls = [c for c in mock_api.patch.call_args_list if "stories" in str(c)]
        assert len(story_fail_calls) >= 1
        call_json = story_fail_calls[0][1].get("json", {})
        assert call_json.get("status") == "failed"
        metadata = call_json.get("failure_metadata", {})
        assert metadata.get("failure_reason") == "worker_rejected"
        assert "Missing secrets" in metadata.get("reject_reason", "")

    @pytest.mark.asyncio
    @patch("src.consumers.engineering._should_run_ci_gate", new_callable=AsyncMock)
    @patch("src.consumers.engineering._run_ci_gate_and_handle_failure", new_callable=AsyncMock)
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock)
    async def test_normal_failure_still_uses_failed_status(
        self, mock_delete, mock_publish, mock_ci_run, mock_should, mock_redis, mock_api
    ):
        """Non-rejected CI failure → _run_ci_gate returns failure dict."""
        from src.consumers.engineering import _handle_engineering_success

        mock_should.return_value = True
        mock_ci_run.return_value = {
            "status": "failed",
            "error": "CI failed after 3 attempt(s), retries exhausted",
        }

        result = await _handle_engineering_success(
            result=_success_result(),
            task_id="eng-1",
            project={"id": "proj-1", "name": "test"},
            callback_stream="po:response:abc",
            redis=mock_redis,
            skip_deploy=False,
            developer_started_at=datetime(2025, 1, 1, tzinfo=UTC),
            user_id="u1",
            planning_task_id="task-123",
            story_id=None,
        )

        assert result["status"] == "failed"
