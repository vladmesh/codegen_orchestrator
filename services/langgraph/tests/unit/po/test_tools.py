"""Unit tests for PO tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.po.tools import (
    create_project,
    get_all_tools,
    get_project,
    get_task_status,
    init_po_clients,
    list_projects,
    set_project_secret,
    set_reminder,
    trigger_deploy,
    trigger_engineering,
)


@pytest.fixture(autouse=True)
def _init_clients(mock_api_client, mock_redis):
    """Initialize PO tools with mock clients for every test."""
    init_po_clients(mock_api_client, mock_redis)


@pytest.fixture
def mock_api_client():
    """Mock httpx.AsyncClient with async get/post/patch."""
    client = AsyncMock(spec=httpx.AsyncClient)
    return client


@pytest.fixture
def mock_redis():
    """Mock Redis client."""
    redis = AsyncMock()
    return redis


def _make_response(data, status_code: int = 200) -> MagicMock:
    """Create a mock httpx.Response (sync .json() and .raise_for_status())."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = data
    return resp


class TestCreateProject:
    @pytest.mark.asyncio
    async def test_creates_project_with_modules(self, mock_api_client):
        project_data = {"id": "abc123", "name": "my-bot"}
        mock_api_client.post.return_value = _make_response(project_data)

        result = await create_project.ainvoke(
            {"name": "my-bot", "modules": "backend,tg_bot", "description": "A test bot"}
        )

        mock_api_client.post.assert_called_once()
        call_args = mock_api_client.post.call_args
        assert call_args[0][0] == "/api/projects/"
        payload = call_args[1]["json"]
        assert payload["name"] == "my-bot"
        assert "backend" in payload["config"]["modules"]
        assert "tg_bot" in payload["config"]["modules"]
        assert "Project created" in result
        assert "abc123" in result

    @pytest.mark.asyncio
    async def test_ensures_backend_module(self, mock_api_client):
        mock_api_client.post.return_value = _make_response({"id": "x", "name": "y"})

        await create_project.ainvoke({"name": "test", "modules": "tg_bot"})

        payload = mock_api_client.post.call_args[1]["json"]
        assert "backend" in payload["config"]["modules"]

    @pytest.mark.asyncio
    async def test_rejects_invalid_modules(self, mock_api_client):
        result = await create_project.ainvoke({"name": "test", "modules": "invalid_mod"})
        assert "Error" in result
        assert "invalid_mod" in result
        mock_api_client.post.assert_not_called()


class TestListProjects:
    @pytest.mark.asyncio
    async def test_lists_projects(self, mock_api_client):
        mock_api_client.get.return_value = _make_response(
            [
                {"id": "1", "name": "proj-a", "status": "active"},
                {"id": "2", "name": "proj-b", "status": "draft"},
            ]
        )

        result = await list_projects.ainvoke({})

        assert "proj-a" in result
        assert "proj-b" in result
        mock_api_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_list(self, mock_api_client):
        mock_api_client.get.return_value = _make_response([])

        result = await list_projects.ainvoke({})
        assert "No projects" in result


class TestGetProject:
    @pytest.mark.asyncio
    async def test_gets_project(self, mock_api_client):
        project = {"id": "abc", "name": "my-bot", "status": "active"}
        mock_api_client.get.return_value = _make_response(project)

        result = await get_project.ainvoke({"project_id": "abc"})

        parsed = json.loads(result)
        assert parsed["name"] == "my-bot"
        mock_api_client.get.assert_called_once()
        assert "/api/projects/abc" in mock_api_client.get.call_args[0][0]


class TestSetProjectSecret:
    @pytest.mark.asyncio
    async def test_sets_secret(self, mock_api_client):
        mock_api_client.get.return_value = _make_response(
            {"id": "abc", "config": {"modules": ["backend"]}}
        )
        mock_api_client.patch.return_value = _make_response({"id": "abc"})

        result = await set_project_secret.ainvoke(
            {"project_id": "abc", "key": "TELEGRAM_BOT_TOKEN", "value": "123:ABC"}
        )

        assert "Secret" in result
        patch_payload = mock_api_client.patch.call_args[1]["json"]
        assert patch_payload["config"]["secrets"]["TELEGRAM_BOT_TOKEN"] == "123:ABC"  # noqa: S105


class TestTriggerEngineering:
    @pytest.mark.asyncio
    async def test_triggers_engineering(self, mock_api_client, mock_redis):
        mock_api_client.post.return_value = _make_response({"id": "eng-xxx"})

        result = await trigger_engineering.ainvoke({"project_id": "abc"})

        assert "Engineering task queued" in result
        mock_api_client.post.assert_called_once()
        mock_redis.xadd.assert_called_once()

        # Verify queue message
        xadd_args = mock_redis.xadd.call_args
        assert xadd_args[0][0] == "engineering:queue"
        queue_data = json.loads(xadd_args[0][1]["data"])
        assert queue_data["project_id"] == "abc"

    @pytest.mark.asyncio
    async def test_requires_description_for_feature(self, mock_api_client, mock_redis):
        result = await trigger_engineering.ainvoke({"project_id": "abc", "action": "feature"})

        assert "Error" in result
        mock_api_client.post.assert_not_called()


class TestTriggerDeploy:
    @pytest.mark.asyncio
    async def test_triggers_deploy(self, mock_api_client, mock_redis):
        mock_api_client.post.return_value = _make_response({"id": "deploy-xxx"})

        result = await trigger_deploy.ainvoke({"project_id": "abc"})

        assert "Deploy task queued" in result
        mock_redis.xadd.assert_called_once()
        xadd_args = mock_redis.xadd.call_args
        assert xadd_args[0][0] == "deploy:queue"


class TestGetTaskStatus:
    @pytest.mark.asyncio
    async def test_gets_status(self, mock_api_client):
        task = {"id": "eng-123", "status": "completed", "type": "engineering"}
        mock_api_client.get.return_value = _make_response(task)

        result = await get_task_status.ainvoke({"task_id": "eng-123"})

        parsed = json.loads(result)
        assert parsed["status"] == "completed"


class TestSetReminder:
    @pytest.mark.asyncio
    async def test_sets_reminder(self, mock_redis):
        result = await set_reminder.ainvoke(
            {"user_id": "user-1", "delay_minutes": 10, "reason": "check eng task"}
        )

        assert "Reminder set" in result
        mock_redis.zadd.assert_called_once()
        call_args = mock_redis.zadd.call_args
        assert call_args[0][0] == "po:reminders"


class TestGetAllTools:
    def test_returns_all_tools(self):
        tools = get_all_tools()
        expected_count = 8
        assert len(tools) == expected_count

    def test_tool_names(self):
        tools = get_all_tools()
        names = {t.name for t in tools}
        assert names == {
            "create_project",
            "list_projects",
            "get_project",
            "set_project_secret",
            "trigger_engineering",
            "trigger_deploy",
            "get_task_status",
            "set_reminder",
        }
