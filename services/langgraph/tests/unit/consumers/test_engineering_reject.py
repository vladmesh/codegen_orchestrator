"""Unit tests for engineering consumer handling of worker reject.

_handle_worker_reject is called when infrastructure issues are detected.
It must:
- Transition planning task to 'failed' with failure_metadata.failure_reason='worker_rejected'
- Fail the story with reject metadata
- Call notify_admins with reject reason
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def mock_api():
    with patch("src.consumers.engineering.api_client") as api:
        api.patch = AsyncMock()
        api.post = AsyncMock()
        api.get = AsyncMock(return_value={"created_by": "system"})
        yield api


class TestRejectHandling:
    """Tests for _handle_worker_reject."""

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers.engineering.notify_admins", new_callable=AsyncMock)
    async def test_rejected_calls_notify_admins(self, mock_notify, mock_publish, mock_api):
        """Worker reject → notify_admins called."""
        from src.consumers.engineering import _handle_worker_reject

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
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers.engineering.notify_admins", new_callable=AsyncMock)
    async def test_rejected_fails_story_with_metadata(self, mock_notify, mock_publish, mock_api):
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

        story_fail_calls = [c for c in mock_api.patch.call_args_list if "stories" in str(c)]
        assert len(story_fail_calls) >= 1
        call_json = story_fail_calls[0][1].get("json", {})
        assert call_json.get("status") == "failed"
        metadata = call_json.get("failure_metadata", {})
        assert metadata.get("failure_reason") == "worker_rejected"
        assert "Missing secrets" in metadata.get("reject_reason", "")
