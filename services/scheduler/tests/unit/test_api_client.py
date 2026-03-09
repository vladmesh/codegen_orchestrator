"""Unit tests for SchedulerAPIClient — update_task and fail_story methods."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def api_client():
    with patch("src.config.get_settings") as mock_settings:
        settings = MagicMock()
        settings.api_base_url = "http://localhost:8000"
        mock_settings.return_value = settings
        from src.clients.api import SchedulerAPIClient

        return SchedulerAPIClient()


class TestUpdateTask:
    @pytest.mark.asyncio
    async def test_update_task_sends_patch(self, api_client):
        """update_task sends PATCH to /api/tasks/{id} with given data."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "task-1", "current_iteration": 2}
        mock_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.request = AsyncMock(return_value=mock_resp)
        mock_http.is_closed = False
        api_client._client = mock_http

        result = await api_client.update_task("task-1", {"current_iteration": 2})

        mock_http.request.assert_called_once_with(
            "PATCH",
            "/api/tasks/task-1",
            json={"current_iteration": 2},
        )
        assert result["current_iteration"] == 2


class TestFailStory:
    @pytest.mark.asyncio
    async def test_fail_story_posts_to_fail_endpoint(self, api_client):
        """fail_story sends POST to /api/stories/{id}/fail."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "story-1", "status": "failed"}
        mock_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.request = AsyncMock(return_value=mock_resp)
        mock_http.is_closed = False
        api_client._client = mock_http

        result = await api_client.fail_story("story-1")

        mock_http.request.assert_called_once_with(
            "POST",
            "/api/stories/story-1/fail",
            json={"actor": "supervisor"},
        )
        assert result["status"] == "failed"
