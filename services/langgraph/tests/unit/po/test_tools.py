"""Unit tests for PO tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from shared.queues import PO_REMINDERS_KEY
from src.po.tools import (
    create_project,
    get_all_tools,
    get_project,
    get_task_status,
    init_po_clients,
    list_projects,
    notify_user,
    set_project_secret,
    set_reminder,
    trigger_deploy,
    trigger_engineering,
)


@pytest.fixture(autouse=True)
def _init_clients(mock_api_client, mock_stream_client):
    """Initialize PO tools with mock clients for every test."""
    init_po_clients(mock_api_client, mock_stream_client)


@pytest.fixture
def mock_api_client():
    """Mock httpx.AsyncClient with async get/post/patch."""
    client = AsyncMock(spec=httpx.AsyncClient)
    return client


@pytest.fixture
def mock_stream_client():
    """Mock RedisStreamClient."""
    client = AsyncMock()
    client.redis = AsyncMock()
    client.publish_message = AsyncMock()
    client.publish_flat = AsyncMock()
    return client


def _make_response(data, status_code: int = 200) -> MagicMock:
    """Create a mock httpx.Response (sync .json() and .raise_for_status())."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = data
    return resp


def _make_config(user_id: str = "test-user") -> dict:
    """Create a RunnableConfig with user_id."""
    return {"configurable": {"thread_id": f"po-user-{user_id}", "user_id": user_id}}


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
    @patch("src.po.tools.encrypt_dict", side_effect=lambda d: d)
    @patch("src.po.tools.decrypt_dict", side_effect=lambda d: d)
    async def test_sets_secret(self, _mock_decrypt, _mock_encrypt, mock_api_client):
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
    async def test_triggers_engineering(self, mock_api_client, mock_stream_client):
        mock_api_client.post.return_value = _make_response({"id": "eng-xxx"})

        result = await trigger_engineering.ainvoke(
            {"project_id": "abc"}, config=_make_config("user-42")
        )

        assert "Engineering task queued" in result
        mock_api_client.post.assert_called_once()
        mock_stream_client.publish_message.assert_called_once()

        # Verify queue message
        call_args = mock_stream_client.publish_message.call_args
        assert call_args[0][0] == "engineering:queue"
        eng_msg = call_args[0][1]
        assert eng_msg.project_id == "abc"

    @pytest.mark.asyncio
    async def test_requires_description_for_feature(self, mock_api_client, mock_stream_client):
        result = await trigger_engineering.ainvoke(
            {"project_id": "abc", "action": "feature"},
            config=_make_config("user-42"),
        )

        assert "Error" in result
        mock_api_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_po_input_as_callback(self, mock_api_client, mock_stream_client):
        """callback_stream in queue message should be po:input (not po:events:*)."""
        mock_api_client.post.return_value = _make_response({"id": "eng-xxx"})

        await trigger_engineering.ainvoke({"project_id": "abc"}, config=_make_config("user-42"))

        eng_msg = mock_stream_client.publish_message.call_args[0][1]
        assert eng_msg.callback_stream == "po:input"

    @pytest.mark.asyncio
    async def test_passes_real_user_id(self, mock_api_client, mock_stream_client):
        """user_id in queue message should come from config, not hardcoded."""
        mock_api_client.post.return_value = _make_response({"id": "eng-xxx"})

        await trigger_engineering.ainvoke({"project_id": "abc"}, config=_make_config("user-777"))

        eng_msg = mock_stream_client.publish_message.call_args[0][1]
        assert eng_msg.user_id == "user-777"


class TestTriggerDeploy:
    @pytest.mark.asyncio
    async def test_triggers_deploy(self, mock_api_client, mock_stream_client):
        mock_api_client.post.return_value = _make_response({"id": "deploy-xxx"})

        result = await trigger_deploy.ainvoke({"project_id": "abc"}, config=_make_config("user-42"))

        assert "Deploy task queued" in result
        mock_stream_client.publish_message.assert_called_once()
        call_args = mock_stream_client.publish_message.call_args
        assert call_args[0][0] == "deploy:queue"
        deploy_msg = call_args[0][1]
        assert deploy_msg.triggered_by == "po"

    @pytest.mark.asyncio
    async def test_uses_po_input_as_callback(self, mock_api_client, mock_stream_client):
        """callback_stream should be po:input."""
        mock_api_client.post.return_value = _make_response({"id": "deploy-xxx"})

        await trigger_deploy.ainvoke({"project_id": "abc"}, config=_make_config("user-42"))

        deploy_msg = mock_stream_client.publish_message.call_args[0][1]
        assert deploy_msg.callback_stream == "po:input"

    @pytest.mark.asyncio
    async def test_passes_real_user_id(self, mock_api_client, mock_stream_client):
        """user_id should come from config."""
        mock_api_client.post.return_value = _make_response({"id": "deploy-xxx"})

        await trigger_deploy.ainvoke({"project_id": "abc"}, config=_make_config("user-999"))

        deploy_msg = mock_stream_client.publish_message.call_args[0][1]
        assert deploy_msg.user_id == "user-999"


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
    async def test_sets_reminder(self, mock_stream_client):
        result = await set_reminder.ainvoke(
            {"delay_minutes": 10, "reason": "check eng task"},
            config=_make_config("user-1"),
        )

        assert "Reminder set" in result
        mock_stream_client.redis.zadd.assert_called_once()
        call_args = mock_stream_client.redis.zadd.call_args
        assert call_args[0][0] == PO_REMINDERS_KEY

    @pytest.mark.asyncio
    async def test_uses_user_id_from_config(self, mock_stream_client):
        """user_id should come from RunnableConfig, not LLM arguments."""
        await set_reminder.ainvoke(
            {"delay_minutes": 5, "reason": "test"},
            config=_make_config("user-777"),
        )

        reminder_json = list(mock_stream_client.redis.zadd.call_args[0][1].keys())[0]
        import json

        reminder = json.loads(reminder_json)
        assert reminder["user_id"] == "user-777"


class TestNotifyUser:
    @pytest.mark.asyncio
    async def test_writes_to_proactive_stream(self, mock_stream_client):
        """Should publish_flat to po:proactive with user_id and text."""
        result = await notify_user.ainvoke(
            {"message": "Your project is ready!"},
            config=_make_config("user-123"),
        )

        assert "Message sent" in result
        mock_stream_client.publish_flat.assert_called_once()
        call_args = mock_stream_client.publish_flat.call_args
        assert call_args[0][0] == "po:proactive"
        assert call_args[0][1]["text"] == "Your project is ready!"
        assert call_args[0][1]["user_id"] == "user-123"

    @pytest.mark.asyncio
    async def test_uses_user_id_from_config(self, mock_stream_client):
        """user_id should come from RunnableConfig."""
        await notify_user.ainvoke(
            {"message": "test"},
            config=_make_config("user-456"),
        )

        fields = mock_stream_client.publish_flat.call_args[0][1]
        assert fields["user_id"] == "user-456"


class TestGetAllTools:
    def test_returns_all_tools(self):
        tools = get_all_tools()
        expected_count = 9
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
            "notify_user",
        }
