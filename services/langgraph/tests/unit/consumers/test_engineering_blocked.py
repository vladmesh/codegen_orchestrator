"""Unit tests for engineering consumer handling of developer blocker.

When developer agent reports ## BLOCKED, the engineering consumer must:
- Transition planning task to 'waiting_human_review' with failure_metadata
- Transition story to 'waiting_human_review'
- Call notify_admins with blocker details (level=warning)
- Publish story_blocked event to PO for user notification
- NOT delete worker container (admin may need to inspect)
"""

from __future__ import annotations

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
        api.get = AsyncMock(return_value={"created_by": "user"})
        yield api


class TestBlockedHandling:
    """Tests for _handle_worker_blocked."""

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.notify_admins", new_callable=AsyncMock)
    @patch("src.consumers.engineering.publish_story_event", new_callable=AsyncMock)
    async def test_blocked_returns_blocked_status(
        self, mock_po_event, mock_notify, mock_redis, mock_api
    ):
        """_handle_worker_blocked returns status=blocked."""
        from src.consumers.engineering import _handle_worker_blocked

        mock_notify.return_value = 1

        result = await _handle_worker_blocked(
            task_id="eng-1",
            project_id="proj-1",
            planning_task_id="task-123",
            story_id="story-1",
            block_reason="56/78 image URLs return 404",
            user_id="u1",
            redis=mock_redis,
        )

        assert result["status"] == "blocked"
        assert "56/78" in result["block_reason"]

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.notify_admins", new_callable=AsyncMock)
    @patch("src.consumers.engineering.publish_story_event", new_callable=AsyncMock)
    async def test_blocked_transitions_task_to_whr(
        self, mock_po_event, mock_notify, mock_redis, mock_api
    ):
        """Developer blocker → planning task transitions to waiting_human_review."""
        from src.consumers.engineering import _handle_worker_blocked

        mock_notify.return_value = 1

        await _handle_worker_blocked(
            task_id="eng-1",
            project_id="proj-1",
            planning_task_id="task-123",
            story_id="story-1",
            block_reason="Missing credentials",
            user_id="u1",
            redis=mock_redis,
        )

        # Check task transition to WHR
        task_transition_calls = [c for c in mock_api.post.call_args_list if "transition" in str(c)]
        assert len(task_transition_calls) >= 1
        call_params = task_transition_calls[0][1].get("params", {})
        assert call_params.get("to_status") == "waiting_human_review"

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.notify_admins", new_callable=AsyncMock)
    @patch("src.consumers.engineering.publish_story_event", new_callable=AsyncMock)
    async def test_blocked_transitions_story_to_whr(
        self, mock_po_event, mock_notify, mock_redis, mock_api
    ):
        """Developer blocker → story transitions to waiting_human_review."""
        from src.consumers.engineering import _handle_worker_blocked

        mock_notify.return_value = 1

        await _handle_worker_blocked(
            task_id="eng-1",
            project_id="proj-1",
            planning_task_id="task-123",
            story_id="story-1",
            block_reason="API key missing",
            user_id="u1",
            redis=mock_redis,
        )

        # Check story patched to WHR
        story_patch_calls = [c for c in mock_api.patch.call_args_list if "stories" in str(c)]
        assert len(story_patch_calls) >= 1
        call_json = story_patch_calls[0][1].get("json", {})
        assert call_json.get("status") == "waiting_human_review"

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.notify_admins", new_callable=AsyncMock)
    @patch("src.consumers.engineering.publish_story_event", new_callable=AsyncMock)
    async def test_blocked_notifies_admin(self, mock_po_event, mock_notify, mock_redis, mock_api):
        """Developer blocker → admin notified with warning level."""
        from src.consumers.engineering import _handle_worker_blocked

        mock_notify.return_value = 1

        await _handle_worker_blocked(
            task_id="eng-1",
            project_id="proj-1",
            planning_task_id="task-123",
            story_id="story-1",
            block_reason="URLs return 404",
            user_id="u1",
            redis=mock_redis,
        )

        mock_notify.assert_awaited_once()
        call_args = mock_notify.call_args
        assert "404" in call_args[0][0]
        assert call_args[1]["level"] == "warning"

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.notify_admins", new_callable=AsyncMock)
    @patch("src.consumers.engineering.publish_story_event", new_callable=AsyncMock)
    async def test_blocked_notifies_user_via_po(
        self, mock_po_event, mock_notify, mock_redis, mock_api
    ):
        """Developer blocker → PO receives story_blocked event."""
        from src.consumers.engineering import _handle_worker_blocked

        mock_notify.return_value = 1

        await _handle_worker_blocked(
            task_id="eng-1",
            project_id="proj-1",
            planning_task_id="task-123",
            story_id="story-1",
            block_reason="External API down",
            user_id="u1",
            redis=mock_redis,
        )

        mock_po_event.assert_awaited_once()
        call_kwargs = mock_po_event.call_args[1]
        assert call_kwargs["event"] == "story_blocked"
        assert call_kwargs["user_id"] == "u1"

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.notify_admins", new_callable=AsyncMock)
    @patch("src.consumers.engineering.publish_story_event", new_callable=AsyncMock)
    async def test_blocked_sets_failure_metadata(
        self, mock_po_event, mock_notify, mock_redis, mock_api
    ):
        """Developer blocker → task gets failure_metadata with developer_blocked reason."""
        from src.consumers.engineering import _handle_worker_blocked

        mock_notify.return_value = 1

        await _handle_worker_blocked(
            task_id="eng-1",
            project_id="proj-1",
            planning_task_id="task-123",
            story_id="story-1",
            block_reason="Contradictory requirements",
            user_id="u1",
            redis=mock_redis,
        )

        # Find the patch call that sets failure_metadata on the task
        task_metadata_calls = [
            c
            for c in mock_api.patch.call_args_list
            if "tasks" in str(c) and "failure_metadata" in str(c)
        ]
        assert len(task_metadata_calls) >= 1
        metadata = task_metadata_calls[0][1]["json"]["failure_metadata"]
        assert metadata["failure_reason"] == "developer_blocked"
        assert "Contradictory" in metadata["block_reason"]

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.notify_admins", new_callable=AsyncMock)
    @patch("src.consumers.engineering.publish_story_event", new_callable=AsyncMock)
    async def test_blocked_without_story_skips_story_transition(
        self, mock_po_event, mock_notify, mock_redis, mock_api
    ):
        """Developer blocker without story_id → no story transition."""
        from src.consumers.engineering import _handle_worker_blocked

        mock_notify.return_value = 1

        await _handle_worker_blocked(
            task_id="eng-1",
            project_id="proj-1",
            planning_task_id="task-123",
            story_id=None,
            block_reason="Some issue",
            user_id="u1",
            redis=mock_redis,
        )

        story_patch_calls = [c for c in mock_api.patch.call_args_list if "stories" in str(c)]
        assert len(story_patch_calls) == 0
