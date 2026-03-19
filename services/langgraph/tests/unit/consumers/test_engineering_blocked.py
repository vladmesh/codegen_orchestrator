"""Unit tests for engineering consumer handling of worker gave_up.

When a worker gives up (blocker or reject), the engineering consumer must:
- Transition planning task to 'waiting_human_review' with failure_metadata
- Transition story to 'waiting_human_review'
- Call notify_admins with reason details (level=warning)
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
    with patch("src.consumers.engineering_result_handler.api_client") as api:
        api.patch = AsyncMock()
        api.post = AsyncMock()
        api.get = AsyncMock(return_value={"created_by": "user"})
        yield api


class TestGaveUpHandling:
    """Tests for handle_worker_gave_up (merged blocked + reject)."""

    @pytest.mark.asyncio
    @patch("src.consumers.engineering_result_handler.notify_admins", new_callable=AsyncMock)
    @patch("src.consumers.engineering_result_handler.publish_story_event", new_callable=AsyncMock)
    async def test_gave_up_returns_gave_up_status(
        self, mock_po_event, mock_notify, mock_redis, mock_api
    ):
        """handle_worker_gave_up returns status=gave_up."""
        from src.consumers.engineering import _handle_worker_gave_up

        mock_notify.return_value = 1

        result = await _handle_worker_gave_up(
            task_id="eng-1",
            project_id="proj-1",
            planning_task_id="task-123",
            story_id="story-1",
            reason="56/78 image URLs return 404",
            user_id="u1",
            redis=mock_redis,
        )

        assert result["status"] == "gave_up"
        assert "56/78" in result["reason"]

    @pytest.mark.asyncio
    @patch("src.consumers.engineering_result_handler.notify_admins", new_callable=AsyncMock)
    @patch("src.consumers.engineering_result_handler.publish_story_event", new_callable=AsyncMock)
    async def test_gave_up_transitions_task_to_whr(
        self, mock_po_event, mock_notify, mock_redis, mock_api
    ):
        """Worker gave_up → planning task transitions to waiting_human_review."""
        from src.consumers.engineering import _handle_worker_gave_up

        mock_notify.return_value = 1

        await _handle_worker_gave_up(
            task_id="eng-1",
            project_id="proj-1",
            planning_task_id="task-123",
            story_id="story-1",
            reason="Missing credentials",
            user_id="u1",
            redis=mock_redis,
        )

        task_transition_calls = [c for c in mock_api.post.call_args_list if "transition" in str(c)]
        assert len(task_transition_calls) >= 1
        call_params = task_transition_calls[0][1].get("params", {})
        assert call_params.get("to_status") == "waiting_human_review"

    @pytest.mark.asyncio
    @patch("src.consumers.engineering_result_handler.notify_admins", new_callable=AsyncMock)
    @patch("src.consumers.engineering_result_handler.publish_story_event", new_callable=AsyncMock)
    async def test_gave_up_transitions_story_to_whr(
        self, mock_po_event, mock_notify, mock_redis, mock_api
    ):
        """Worker gave_up → story transitions to waiting_human_review."""
        from src.consumers.engineering import _handle_worker_gave_up

        mock_notify.return_value = 1

        await _handle_worker_gave_up(
            task_id="eng-1",
            project_id="proj-1",
            planning_task_id="task-123",
            story_id="story-1",
            reason="API key missing",
            user_id="u1",
            redis=mock_redis,
        )

        story_patch_calls = [c for c in mock_api.patch.call_args_list if "stories" in str(c)]
        assert len(story_patch_calls) >= 1
        call_json = story_patch_calls[0][1].get("json", {})
        assert call_json.get("status") == "waiting_human_review"

    @pytest.mark.asyncio
    @patch("src.consumers.engineering_result_handler.notify_admins", new_callable=AsyncMock)
    @patch("src.consumers.engineering_result_handler.publish_story_event", new_callable=AsyncMock)
    async def test_gave_up_notifies_admin(self, mock_po_event, mock_notify, mock_redis, mock_api):
        """Worker gave_up → admin notified with warning level."""
        from src.consumers.engineering import _handle_worker_gave_up

        mock_notify.return_value = 1

        await _handle_worker_gave_up(
            task_id="eng-1",
            project_id="proj-1",
            planning_task_id="task-123",
            story_id="story-1",
            reason="URLs return 404",
            user_id="u1",
            redis=mock_redis,
        )

        mock_notify.assert_awaited_once()
        call_args = mock_notify.call_args
        assert "404" in call_args[0][0]
        assert call_args[1]["level"] == "warning"

    @pytest.mark.asyncio
    @patch("src.consumers.engineering_result_handler.notify_admins", new_callable=AsyncMock)
    @patch("src.consumers.engineering_result_handler.publish_story_event", new_callable=AsyncMock)
    async def test_gave_up_notifies_user_via_po(
        self, mock_po_event, mock_notify, mock_redis, mock_api
    ):
        """Worker gave_up → PO receives story_blocked event."""
        from src.consumers.engineering import _handle_worker_gave_up

        mock_notify.return_value = 1

        await _handle_worker_gave_up(
            task_id="eng-1",
            project_id="proj-1",
            planning_task_id="task-123",
            story_id="story-1",
            reason="External API down",
            user_id="u1",
            redis=mock_redis,
        )

        mock_po_event.assert_awaited_once()
        call_kwargs = mock_po_event.call_args[1]
        assert call_kwargs["event"] == "story_blocked"
        assert call_kwargs["user_id"] == "u1"

    @pytest.mark.asyncio
    @patch("src.consumers.engineering_result_handler.notify_admins", new_callable=AsyncMock)
    @patch("src.consumers.engineering_result_handler.publish_story_event", new_callable=AsyncMock)
    async def test_gave_up_sets_failure_metadata(
        self, mock_po_event, mock_notify, mock_redis, mock_api
    ):
        """Worker gave_up → task gets failure_metadata with reason."""
        from src.consumers.engineering import _handle_worker_gave_up

        mock_notify.return_value = 1

        await _handle_worker_gave_up(
            task_id="eng-1",
            project_id="proj-1",
            planning_task_id="task-123",
            story_id="story-1",
            reason="Contradictory requirements",
            user_id="u1",
            redis=mock_redis,
        )

        task_metadata_calls = [
            c
            for c in mock_api.patch.call_args_list
            if "tasks" in str(c) and "failure_metadata" in str(c)
        ]
        assert len(task_metadata_calls) >= 1
        metadata = task_metadata_calls[0][1]["json"]["failure_metadata"]
        assert "Contradictory" in metadata["reason"]

    @pytest.mark.asyncio
    @patch("src.consumers.engineering_result_handler.notify_admins", new_callable=AsyncMock)
    @patch("src.consumers.engineering_result_handler.publish_story_event", new_callable=AsyncMock)
    async def test_gave_up_without_story_skips_story_transition(
        self, mock_po_event, mock_notify, mock_redis, mock_api
    ):
        """Worker gave_up without story_id → no story transition."""
        from src.consumers.engineering import _handle_worker_gave_up

        mock_notify.return_value = 1

        await _handle_worker_gave_up(
            task_id="eng-1",
            project_id="proj-1",
            planning_task_id="task-123",
            story_id=None,
            reason="Some issue",
            user_id="u1",
            redis=mock_redis,
        )

        story_patch_calls = [c for c in mock_api.patch.call_args_list if "stories" in str(c)]
        assert len(story_patch_calls) == 0
