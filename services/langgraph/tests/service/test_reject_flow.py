"""Service tests for worker reject flow — real Redis, mocked API.

Tests the full chain: CI gate returns rejected → _handle_engineering_success
→ _handle_worker_reject → task failed with metadata → story failed →
admin notified → callback event published to real Redis stream.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from shared.redis_client import RedisStreamClient

CALLBACK_STREAM = "po:response:reject-test"


@pytest.fixture
async def stream_redis(real_redis):
    """RedisStreamClient backed by real Redis."""
    client = RedisStreamClient.__new__(RedisStreamClient)
    client._redis = real_redis
    # Clean up test stream before and after
    await real_redis.delete(CALLBACK_STREAM)
    yield client
    await real_redis.delete(CALLBACK_STREAM)


@pytest.fixture
def mock_api():
    with patch("src.consumers.engineering.api_client") as api:
        api.patch = AsyncMock()
        api.post = AsyncMock()
        api.get = AsyncMock(return_value={"created_by": "system"})
        api.get_project = AsyncMock(
            return_value={
                "id": "proj-reject",
                "name": "reject-test",
                "status": "active",
                "config": {"modules": ["backend"]},
            }
        )
        api.get_primary_repository = AsyncMock(
            return_value={"git_url": "https://github.com/org/reject-test"}
        )
        yield api


class TestRejectFlowReal:
    """Full reject chain with real Redis streams."""

    @pytest.mark.asyncio
    @patch("src.consumers.engineering._wait_for_ci_and_fix", new_callable=AsyncMock)
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering.notify_admins", new_callable=AsyncMock)
    async def test_reject_publishes_callback_and_patches_api(
        self,
        mock_notify,
        mock_delete,
        mock_publish,
        mock_ci_gate,
        stream_redis,
        mock_api,
        real_redis,
    ):
        """Worker reject → API patched correctly, admin notified, result correct."""
        from src.consumers.engineering import _handle_engineering_success

        reject_reason = "REGISTRY_PASSWORD secret is empty — cannot push Docker image"
        mock_ci_gate.return_value = (
            False,
            [{"attempt": 0, "status": "rejected"}],
            True,
            reject_reason,
        )
        mock_notify.return_value = 1

        result = await _handle_engineering_success(
            result={
                "engineering_status": "done",
                "commit_sha": "abc123",
                "worker_id": "dev-reject-1",
            },
            task_id="eng-reject-1",
            project={"id": "proj-reject", "name": "reject-test"},
            callback_stream=CALLBACK_STREAM,
            redis=stream_redis,
            skip_deploy=False,
            developer_started_at=datetime(2025, 6, 1, tzinfo=UTC),
            user_id="u-test",
            planning_task_id="task-reject-1",
            story_id="story-reject-1",
        )

        # --- Result dict ---
        assert result["status"] == "failed"
        assert result["rejected"] is True
        assert "REGISTRY_PASSWORD" in result["reject_reason"]

        # --- Engineering run marked failed ---
        run_patch = next(c for c in mock_api.patch.call_args_list if "runs/eng-reject-1" in str(c))
        assert "Worker rejected" in run_patch[1]["json"]["error_message"]

        # --- Planning task → failed ---
        task_transition = next(
            c for c in mock_api.post.call_args_list if "tasks/task-reject-1/transition" in str(c)
        )
        assert "failed" in str(task_transition)

        # --- Planning task gets failure_metadata ---
        task_patch = next(
            c for c in mock_api.patch.call_args_list if "tasks/task-reject-1" in str(c)
        )
        metadata = task_patch[1]["json"]["failure_metadata"]
        assert metadata["failure_reason"] == "worker_rejected"
        assert "REGISTRY_PASSWORD" in metadata["reject_reason"]

        # --- Story → failed with metadata ---
        story_patch = next(
            c for c in mock_api.patch.call_args_list if "stories/story-reject-1" in str(c)
        )
        story_json = story_patch[1]["json"]
        assert story_json["status"] == "failed"
        assert story_json["failure_metadata"]["failure_reason"] == "worker_rejected"

        # --- Admin notified ---
        mock_notify.assert_awaited_once()
        notify_msg = mock_notify.call_args[0][0]
        assert "eng-reject-1" in notify_msg
        assert "REGISTRY_PASSWORD" in notify_msg
        assert mock_notify.call_args[1]["level"] == "error"

    @pytest.mark.asyncio
    @patch("src.consumers.engineering._wait_for_ci_and_fix", new_callable=AsyncMock)
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering.notify_admins", new_callable=AsyncMock)
    async def test_reject_without_story_skips_story_patch(
        self,
        mock_notify,
        mock_delete,
        mock_publish,
        mock_ci_gate,
        stream_redis,
        mock_api,
    ):
        """Standalone task reject (no story_id) → no story patch, still notifies admin."""
        from src.consumers.engineering import _handle_engineering_success

        mock_ci_gate.return_value = (
            False,
            [{"attempt": 0, "status": "rejected"}],
            True,
            "Docker build fails: base image not found",
        )
        mock_notify.return_value = 1

        result = await _handle_engineering_success(
            result={
                "engineering_status": "done",
                "commit_sha": "def456",
                "worker_id": "dev-reject-2",
            },
            task_id="eng-reject-2",
            project={"id": "proj-reject", "name": "reject-test"},
            callback_stream=CALLBACK_STREAM,
            redis=stream_redis,
            skip_deploy=False,
            developer_started_at=datetime(2025, 6, 1, tzinfo=UTC),
            user_id="u-test",
            planning_task_id="task-reject-2",
            story_id=None,
        )

        assert result["status"] == "failed"
        assert result["rejected"] is True

        # No story patch
        story_patches = [c for c in mock_api.patch.call_args_list if "stories/" in str(c)]
        assert len(story_patches) == 0

        # Task still transitions to failed with metadata
        task_transition = next(
            c for c in mock_api.post.call_args_list if "tasks/task-reject-2/transition" in str(c)
        )
        assert "failed" in str(task_transition)

        # Admin still notified
        mock_notify.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("src.consumers.engineering._wait_for_ci_and_fix", new_callable=AsyncMock)
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock)
    async def test_normal_ci_failure_does_not_trigger_reject_path(
        self,
        mock_delete,
        mock_publish,
        mock_ci_gate,
        stream_redis,
        mock_api,
    ):
        """Normal CI failure (not rejected) → task=failed, no reject metadata."""
        from src.consumers.engineering import _handle_engineering_success

        mock_ci_gate.return_value = (
            False,
            [{"attempt": 0, "status": "failed"}],
            False,
            None,
        )

        result = await _handle_engineering_success(
            result={
                "engineering_status": "done",
                "commit_sha": "ghi789",
                "worker_id": "dev-normal-1",
            },
            task_id="eng-normal-1",
            project={"id": "proj-reject", "name": "reject-test"},
            callback_stream=CALLBACK_STREAM,
            redis=stream_redis,
            skip_deploy=False,
            developer_started_at=datetime(2025, 6, 1, tzinfo=UTC),
            user_id="u-test",
            planning_task_id="task-normal-1",
            story_id=None,
        )

        assert result["status"] == "failed"
        assert "rejected" not in result

        # Task goes to failed (normal path)
        transition_calls = [c for c in mock_api.post.call_args_list if "transition" in str(c)]
        assert any("failed" in str(c) for c in transition_calls)

        # No failure_metadata patch on task (normal failure doesn't set it)
        task_metadata_patches = [
            c
            for c in mock_api.patch.call_args_list
            if "tasks/" in str(c) and "failure_metadata" in str(c)
        ]
        assert len(task_metadata_patches) == 0
