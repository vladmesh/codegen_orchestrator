"""Unit tests for engineering consumer handling of worker reject → gave_up.

When infrastructure issues are detected (worker rejects), the unified
handle_worker_gave_up handler is called. It must:
- Transition planning task to 'waiting_human_review' with failure_metadata
- Transition story to 'waiting_human_review'
- Call notify_admins with reason
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def mock_api():
    with patch("src.consumers.engineering_result_handler.api_client") as api:
        api.patch = AsyncMock()
        api.post = AsyncMock()
        api.get = AsyncMock(return_value={"created_by": "system"})
        yield api


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.redis = AsyncMock()
    r.redis.xadd = AsyncMock()
    r.publish_flat = AsyncMock()
    return r


class TestRejectGaveUpHandling:
    """Tests for reject-originated gave_up handling."""

    @pytest.mark.asyncio
    @patch(
        "src.consumers.engineering_result_handler.notify_admins_best_effort", new_callable=AsyncMock
    )
    @patch("src.consumers.engineering_result_handler.publish_story_event", new_callable=AsyncMock)
    async def test_reject_calls_notify_admins(
        self, mock_po_event, mock_notify, mock_redis, mock_api
    ):
        """Worker reject (gave_up) → notify_admins called with warning level."""
        from src.consumers.engineering import _handle_worker_gave_up

        mock_notify.return_value = 1

        await _handle_worker_gave_up(
            task_id="eng-1",
            project_id="proj-1",
            planning_task_id="task-123",
            story_id="story-1",
            reason="Docker registry TLS cert is self-signed",
            user_id="u1",
            redis=mock_redis,
        )

        mock_notify.assert_awaited_once()
        call_args = mock_notify.call_args
        assert "TLS cert" in call_args[0][0]
        assert call_args[1]["level"] == "warning"

    @pytest.mark.asyncio
    @patch(
        "src.consumers.engineering_result_handler.notify_admins_best_effort", new_callable=AsyncMock
    )
    @patch("src.consumers.engineering_result_handler.publish_story_event", new_callable=AsyncMock)
    async def test_reject_transitions_story_to_whr(
        self, mock_po_event, mock_notify, mock_redis, mock_api
    ):
        """Worker reject (gave_up) → story transitions to waiting_human_review."""
        from src.consumers.engineering import _handle_worker_gave_up

        mock_notify.return_value = 1

        await _handle_worker_gave_up(
            task_id="eng-1",
            project_id="proj-1",
            planning_task_id="task-123",
            story_id="story-1",
            reason="Missing secrets",
            user_id="u1",
            redis=mock_redis,
        )

        story_patch_calls = [c for c in mock_api.patch.call_args_list if "stories" in str(c)]
        assert len(story_patch_calls) >= 1
        call_json = story_patch_calls[0][1].get("json", {})
        assert call_json.get("status") == "waiting_human_review"
