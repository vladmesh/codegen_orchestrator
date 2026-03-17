"""Unit tests for LanggraphAPIClient architect methods (story/task)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import httpx
import pytest


@pytest.fixture
def mock_httpx_client():
    client = AsyncMock(spec=httpx.AsyncClient)
    client.is_closed = False
    return client


@pytest.fixture
def api_client(mock_httpx_client):
    with patch("src.clients.api.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(api_base_url="http://api:8000")
        from src.clients.api import LanggraphAPIClient

        c = LanggraphAPIClient()
        c._client = mock_httpx_client
        return c


_NOW = datetime.now(UTC).isoformat()
_UUID = str(uuid.uuid4())


def _ok_response(data):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = data
    return resp


def _story_dict(**overrides):
    base = {
        "id": "story-abc",
        "project_id": _UUID,
        "title": "Add auth",
        "type": "product",
        "status": "created",
        "priority": 0,
        "created_by": "system",
        "created_at": _NOW,
    }
    base.update(overrides)
    return base


def _task_dict(**overrides):
    base = {
        "id": "task-1",
        "project_id": _UUID,
        "type": "feature",
        "title": "Test task",
        "status": "todo",
        "priority": 0,
        "current_iteration": 1,
        "max_iterations": 3,
        "created_by": "system",
        "created_at": _NOW,
    }
    base.update(overrides)
    return base


class TestGetStory:
    @pytest.mark.asyncio
    async def test_returns_story_dto(self, api_client, mock_httpx_client):
        mock_httpx_client.request.return_value = _ok_response(_story_dict())

        result = await api_client.get_story("story-abc")

        assert result.id == "story-abc"
        assert result.title == "Add auth"
        call_args = mock_httpx_client.request.call_args
        assert "/api/stories/story-abc" in str(call_args)


class TestGetTasksByStory:
    @pytest.mark.asyncio
    async def test_returns_task_list(self, api_client, mock_httpx_client):
        tasks = [_task_dict(id="task-1"), _task_dict(id="task-2")]
        mock_httpx_client.request.return_value = _ok_response(tasks)

        result = await api_client.get_tasks_by_story("story-abc")

        assert len(result) == 2
        assert result[0].id == "task-1"
        assert result[1].id == "task-2"
        call_args = mock_httpx_client.request.call_args
        assert "story_id" in str(call_args)


class TestCreateTask:
    @pytest.mark.asyncio
    async def test_creates_and_returns_task(self, api_client, mock_httpx_client):
        created = _task_dict(id="task-new", title="New task")
        mock_httpx_client.request.return_value = _ok_response(created)

        task_data = {
            "title": "New task",
            "description": "Do something",
            "project_id": _UUID,
            "story_id": "story-abc",
        }
        result = await api_client.create_task(task_data)

        assert result.id == "task-new"
        assert result.title == "New task"
        call_args = mock_httpx_client.request.call_args
        assert call_args[0][0] == "POST"
        assert "/api/tasks/" in str(call_args)


class TestTransitionStory:
    @pytest.mark.asyncio
    async def test_transitions_story(self, api_client, mock_httpx_client):
        mock_httpx_client.request.return_value = _ok_response(_story_dict(status="in_progress"))

        result = await api_client.transition_story("story-abc", "start")

        assert result.status == "in_progress"
        call_args = mock_httpx_client.request.call_args
        assert "story-abc" in str(call_args)
        assert "start" in str(call_args)
