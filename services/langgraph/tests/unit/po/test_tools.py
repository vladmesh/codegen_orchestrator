"""Unit tests for PO tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from shared.queues import PO_REMINDERS_KEY
from src.agents.po.tools import (
    create_project,
    create_story,
    get_all_tools,
    get_project,
    get_run_status,
    get_story,
    init_po_clients,
    list_projects,
    list_stories,
    notify_user,
    set_project_secret,
    set_reminder,
    web_search,
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
            {"name": "my-bot", "modules": "backend,tg_bot", "description": "A test bot"},
            config=_make_config("user-42"),
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
    async def test_passes_telegram_id_header(self, mock_api_client):
        mock_api_client.post.return_value = _make_response({"id": "x", "name": "y"})

        await create_project.ainvoke(
            {"name": "test", "modules": "backend"},
            config=_make_config("12345"),
        )

        call_args = mock_api_client.post.call_args
        headers = call_args[1].get("headers", {})
        assert headers.get("X-Telegram-ID") == "12345"

    @pytest.mark.asyncio
    async def test_ensures_backend_module(self, mock_api_client):
        mock_api_client.post.return_value = _make_response({"id": "x", "name": "y"})

        await create_project.ainvoke(
            {"name": "test", "modules": "tg_bot"},
            config=_make_config("user-1"),
        )

        payload = mock_api_client.post.call_args[1]["json"]
        assert "backend" in payload["config"]["modules"]

    @pytest.mark.asyncio
    async def test_rejects_invalid_modules(self, mock_api_client):
        result = await create_project.ainvoke(
            {"name": "test", "modules": "invalid_mod"},
            config=_make_config("user-1"),
        )
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

        result = await list_projects.ainvoke({}, config=_make_config("user-42"))

        assert "proj-a" in result
        assert "proj-b" in result
        mock_api_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_passes_telegram_id_header(self, mock_api_client):
        mock_api_client.get.return_value = _make_response([])

        await list_projects.ainvoke({}, config=_make_config("99999"))

        call_args = mock_api_client.get.call_args
        headers = call_args[1].get("headers", {})
        assert headers.get("X-Telegram-ID") == "99999"

    @pytest.mark.asyncio
    async def test_empty_list(self, mock_api_client):
        mock_api_client.get.return_value = _make_response([])

        result = await list_projects.ainvoke({}, config=_make_config("user-1"))
        assert "No projects" in result


class TestGetProject:
    @pytest.mark.asyncio
    async def test_gets_project(self, mock_api_client):
        project = {"id": "abc", "name": "my-bot", "status": "active"}
        mock_api_client.get.return_value = _make_response(project)

        result = await get_project.ainvoke({"project_id": "abc"}, config=_make_config("user-42"))

        parsed = json.loads(result)
        assert parsed["name"] == "my-bot"
        mock_api_client.get.assert_called_once()
        assert "/api/projects/abc" in mock_api_client.get.call_args[0][0]

    @pytest.mark.asyncio
    async def test_passes_telegram_id_header(self, mock_api_client):
        mock_api_client.get.return_value = _make_response({"id": "abc", "name": "x"})

        await get_project.ainvoke({"project_id": "abc"}, config=_make_config("55555"))

        headers = mock_api_client.get.call_args[1].get("headers", {})
        assert headers.get("X-Telegram-ID") == "55555"


class TestSetProjectSecret:
    @pytest.mark.asyncio
    async def test_sets_secret(self, mock_api_client):
        mock_api_client.post.return_value = _make_response({"keys": ["TELEGRAM_BOT_TOKEN"]})

        result = await set_project_secret.ainvoke(
            {"project_id": "abc", "key": "TELEGRAM_BOT_TOKEN", "value": "123:ABC"},
            config=_make_config("user-42"),
        )

        assert "Secret" in result
        call_args = mock_api_client.post.call_args
        assert call_args[0][0] == "/api/projects/abc/config/secrets"
        payload = call_args[1]["json"]
        assert payload["secrets"]["TELEGRAM_BOT_TOKEN"] == "123:ABC"  # noqa: S105

    @pytest.mark.asyncio
    async def test_passes_telegram_id_header(self, mock_api_client):
        mock_api_client.post.return_value = _make_response({"keys": ["K"]})

        await set_project_secret.ainvoke(
            {"project_id": "abc", "key": "K", "value": "V"},
            config=_make_config("77777"),
        )

        headers = mock_api_client.post.call_args[1].get("headers", {})
        assert headers.get("X-Telegram-ID") == "77777"

    @pytest.mark.asyncio
    async def test_hint_included_in_payload(self, mock_api_client):
        """When hint is provided, it should be sent in env_hints."""
        mock_api_client.post.return_value = _make_response({"keys": ["ADMIN_TELEGRAM_ID"]})

        result = await set_project_secret.ainvoke(
            {
                "project_id": "abc",
                "key": "ADMIN_TELEGRAM_ID",
                "value": "42",
                "hint": "Telegram ID of the bot admin",
            },
            config=_make_config("user-42"),
        )

        assert "Secret" in result
        payload = mock_api_client.post.call_args[1]["json"]
        assert payload["env_hints"]["ADMIN_TELEGRAM_ID"] == "Telegram ID of the bot admin"

    @pytest.mark.asyncio
    async def test_no_hint_no_env_hints(self, mock_api_client):
        """When no hint is provided, env_hints should not be in payload."""
        mock_api_client.post.return_value = _make_response({"keys": ["TOKEN"]})

        await set_project_secret.ainvoke(
            {"project_id": "abc", "key": "TOKEN", "value": "abc123"},
            config=_make_config("user-42"),
        )

        payload = mock_api_client.post.call_args[1]["json"]
        assert "env_hints" not in payload

    @pytest.mark.asyncio
    async def test_no_get_patch_calls(self, mock_api_client):
        """Tool should use single POST, not GET+PATCH (race condition fix)."""
        mock_api_client.post.return_value = _make_response({"keys": ["K"]})

        await set_project_secret.ainvoke(
            {"project_id": "abc", "key": "K", "value": "V"},
            config=_make_config("user-42"),
        )

        mock_api_client.get.assert_not_called()
        mock_api_client.patch.assert_not_called()


class TestCreateStory:
    @pytest.mark.asyncio
    async def test_creates_story_and_publishes_to_architect(
        self, mock_api_client, mock_stream_client
    ):
        """create_story publishes ArchitectMessage to architect:queue."""
        mock_api_client.post.return_value = _make_response({"id": "story-xxx"})
        project_data = {
            "id": "abc",
            "status": "draft",
            "config": {"modules": ["backend"], "name": "my-bot"},
        }
        mock_api_client.get.side_effect = [
            _make_response(project_data),
            _make_response([]),  # no active stories
        ]
        mock_api_client.patch.return_value = _make_response({"id": "abc"})

        result = await create_story.ainvoke(
            {
                "project_id": "abc",
                "title": "Create todo bot",
                "description": "Build a todo app with reminders",
            },
            config=_make_config("user-42"),
        )

        assert "Story created" in result
        assert "architect" in result.lower()

        # Should have 1 POST call: create story only (no run, no start)
        assert mock_api_client.post.call_count == 1
        story_call = mock_api_client.post.call_args_list[0]
        assert story_call[0][0] == "/api/stories/"
        story_payload = story_call[1]["json"]
        assert story_payload["title"] == "Create todo bot"
        assert story_payload["type"] == "product"
        assert story_payload["created_by"] == "po"

        # Should publish ArchitectMessage to architect:queue
        from shared.contracts.queues.architect import ArchitectMessage
        from shared.queues import ARCHITECT_QUEUE

        pub_call = mock_stream_client.publish_message.call_args
        assert pub_call[0][0] == ARCHITECT_QUEUE
        arch_msg = pub_call[0][1]
        assert isinstance(arch_msg, ArchitectMessage)
        assert arch_msg.story_id == "story-xxx"
        assert arch_msg.project_id == "abc"
        assert arch_msg.user_id == "user-42"

    @pytest.mark.asyncio
    async def test_no_run_created(self, mock_api_client, mock_stream_client):
        """create_story should NOT create a Run (dispatcher does that)."""
        mock_api_client.post.return_value = _make_response({"id": "story-xxx"})
        mock_api_client.get.side_effect = [
            _make_response({"id": "abc", "status": "active", "config": {}}),
            _make_response([]),  # no active stories
        ]

        await create_story.ainvoke(
            {
                "project_id": "abc",
                "title": "Add feature",
                "description": "New feature",
            },
            config=_make_config("user-42"),
        )

        # Only 1 POST: story creation. No /api/runs/ call.
        assert mock_api_client.post.call_count == 1
        for call in mock_api_client.post.call_args_list:
            assert "/api/runs/" not in call[0][0]

    @pytest.mark.asyncio
    async def test_persists_description_for_create(self, mock_api_client, mock_stream_client):
        """For action=create, should persist description to project config."""
        mock_api_client.post.return_value = _make_response({"id": "story-xxx"})
        project_resp = _make_response(
            {"id": "abc", "status": "draft", "config": {"modules": ["backend"], "name": "my-bot"}}
        )
        mock_api_client.get.side_effect = [
            project_resp,  # project status check
            _make_response([]),  # no active stories
            project_resp,  # re-fetch for config persist
        ]
        mock_api_client.patch.return_value = _make_response({"id": "abc"})

        await create_story.ainvoke(
            {
                "project_id": "abc",
                "title": "Create new bot",
                "description": "Build a recipe bot",
            },
            config=_make_config("user-42"),
        )

        # Should PATCH project config with detailed_spec
        mock_api_client.patch.assert_called_once()
        patched_config = mock_api_client.patch.call_args[1]["json"]["config"]
        assert patched_config["detailed_spec"] == "Build a recipe bot"

    @pytest.mark.asyncio
    async def test_no_patch_for_feature_on_active(self, mock_api_client, mock_stream_client):
        """For action=feature, should NOT persist description to project config."""
        mock_api_client.post.return_value = _make_response({"id": "story-xxx"})
        mock_api_client.get.side_effect = [
            _make_response({"id": "abc", "status": "active", "config": {}}),
            _make_response([]),  # no active stories
        ]

        await create_story.ainvoke(
            {
                "project_id": "abc",
                "title": "Add feature",
                "description": "New feature",
            },
            config=_make_config("user-42"),
        )

        mock_api_client.patch.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_patch_for_fix(self, mock_api_client, mock_stream_client):
        """For action=fix, should NOT persist description to project config."""
        mock_api_client.post.return_value = _make_response({"id": "story-xxx"})
        mock_api_client.get.return_value = _make_response([])  # no active stories

        await create_story.ainvoke(
            {
                "project_id": "abc",
                "title": "Fix bug",
                "description": "Fix the login",
                "story_type": "fix",
            },
            config=_make_config("user-42"),
        )

        mock_api_client.patch.assert_not_called()

    @pytest.mark.asyncio
    async def test_passes_user_id_to_architect_message(self, mock_api_client, mock_stream_client):
        mock_api_client.post.return_value = _make_response({"id": "story-xxx"})
        mock_api_client.get.side_effect = [
            _make_response({"id": "abc", "status": "draft", "config": {}}),
            _make_response([]),  # no active stories
        ]

        await create_story.ainvoke(
            {
                "project_id": "abc",
                "title": "Test",
                "description": "Test desc",
            },
            config=_make_config("user-777"),
        )

        from shared.contracts.queues.architect import ArchitectMessage

        arch_msg = mock_stream_client.publish_message.call_args[0][1]
        assert isinstance(arch_msg, ArchitectMessage)
        assert arch_msg.user_id == "user-777"

    @pytest.mark.asyncio
    async def test_queues_story_when_active_story_exists(self, mock_api_client, mock_stream_client):
        """If project has in_progress story, create story but don't publish to architect."""
        mock_api_client.post.return_value = _make_response({"id": "story-new"})
        # First GET: project status (active → action=feature)
        # Second GET: stories list (has in_progress story)
        mock_api_client.get.side_effect = [
            _make_response({"id": "abc", "status": "active", "config": {}}),
            _make_response([{"id": "story-old", "status": "in_progress"}]),
        ]

        result = await create_story.ainvoke(
            {
                "project_id": "abc",
                "title": "Add feature",
                "description": "New feature",
            },
            config=_make_config("user-42"),
        )

        # Story created
        assert mock_api_client.post.call_count == 1
        # But NOT published to architect:queue
        mock_stream_client.publish_message.assert_not_called()
        assert "queued" in result.lower()

    @pytest.mark.asyncio
    async def test_publishes_when_no_active_story(self, mock_api_client, mock_stream_client):
        """If project has no in_progress story, publish to architect normally."""
        mock_api_client.post.return_value = _make_response({"id": "story-new"})
        mock_api_client.get.side_effect = [
            _make_response({"id": "abc", "status": "active", "config": {}}),
            _make_response([]),  # No active stories
        ]

        result = await create_story.ainvoke(
            {
                "project_id": "abc",
                "title": "Add feature",
                "description": "New feature",
            },
            config=_make_config("user-42"),
        )

        mock_stream_client.publish_message.assert_called_once()
        assert "architect" in result.lower()


class TestListStories:
    @pytest.mark.asyncio
    async def test_lists_stories(self, mock_api_client):
        mock_api_client.get.return_value = _make_response(
            [
                {"id": "s1", "title": "Create bot", "status": "in_progress", "type": "product"},
                {"id": "s2", "title": "Fix bug", "status": "completed", "type": "product"},
            ]
        )

        result = await list_stories.ainvoke({"project_id": "abc"}, config=_make_config("user-42"))

        assert "Create bot" in result
        assert "Fix bug" in result
        assert "in_progress" in result

    @pytest.mark.asyncio
    async def test_empty_stories(self, mock_api_client):
        mock_api_client.get.return_value = _make_response([])

        result = await list_stories.ainvoke({"project_id": "abc"}, config=_make_config("user-42"))

        assert "No stories" in result


class TestGetStory:
    @pytest.mark.asyncio
    async def test_gets_story_with_tasks(self, mock_api_client):
        story = {"id": "s1", "title": "My story", "status": "in_progress"}
        tasks = [
            {"id": "eng-123", "status": "completed", "type": "engineering"},
            {"id": "eng-456", "status": "running", "type": "engineering"},
        ]
        mock_api_client.get.side_effect = [
            _make_response(story),
            _make_response(tasks),
        ]

        result = await get_story.ainvoke({"story_id": "s1"}, config=_make_config("user-42"))

        parsed = json.loads(result)
        assert parsed["story"]["title"] == "My story"
        assert len(parsed["tasks"]) == 2

        # Verify correct API calls
        calls = mock_api_client.get.call_args_list
        assert "/api/stories/s1" in calls[0][0][0]
        assert "story_id=s1" in calls[1][0][0]


class TestGetRunStatus:
    @pytest.mark.asyncio
    async def test_gets_status(self, mock_api_client):
        run = {"id": "eng-123", "status": "completed", "type": "engineering"}
        mock_api_client.get.return_value = _make_response(run)

        result = await get_run_status.ainvoke({"run_id": "eng-123"}, config=_make_config("user-42"))

        parsed = json.loads(result)
        assert parsed["status"] == "completed"
        assert "/api/runs/eng-123" in mock_api_client.get.call_args[0][0]

    @pytest.mark.asyncio
    async def test_passes_telegram_id_header(self, mock_api_client):
        mock_api_client.get.return_value = _make_response({"id": "eng-1", "status": "running"})

        await get_run_status.ainvoke({"run_id": "eng-1"}, config=_make_config("88888"))

        headers = mock_api_client.get.call_args[1].get("headers", {})
        assert headers.get("X-Telegram-ID") == "88888"


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


class TestWebSearch:
    @pytest.mark.asyncio
    async def test_returns_formatted_results(self):
        mock_results = [
            {
                "title": "OpenWeather API",
                "body": "Free weather API",
                "href": "https://openweathermap.org/api",
            },
            {
                "title": "Weather API Docs",
                "body": "Documentation for weather",
                "href": "https://example.com/docs",
            },
        ]
        mock_ddgs = MagicMock()
        mock_ddgs.text.return_value = mock_results

        with patch("duckduckgo_search.DDGS", return_value=mock_ddgs):
            result = await web_search.ainvoke(
                {"query": "weather API documentation"},
                config=_make_config("user-1"),
            )

        assert "OpenWeather API" in result
        assert "https://openweathermap.org/api" in result
        assert "Weather API Docs" in result
        mock_ddgs.text.assert_called_once_with("weather API documentation", max_results=5)

    @pytest.mark.asyncio
    async def test_custom_max_results(self):
        mock_ddgs = MagicMock()
        mock_ddgs.text.return_value = []

        with patch("duckduckgo_search.DDGS", return_value=mock_ddgs):
            await web_search.ainvoke(
                {"query": "test", "max_results": 3},
                config=_make_config("user-1"),
            )

        mock_ddgs.text.assert_called_once_with("test", max_results=3)

    @pytest.mark.asyncio
    async def test_no_results(self):
        mock_ddgs = MagicMock()
        mock_ddgs.text.return_value = []

        with patch("duckduckgo_search.DDGS", return_value=mock_ddgs):
            result = await web_search.ainvoke(
                {"query": "nonexistent thing xyz"},
                config=_make_config("user-1"),
            )

        assert "No results" in result

    @pytest.mark.asyncio
    async def test_handles_search_error(self):
        mock_ddgs = MagicMock()
        mock_ddgs.text.side_effect = Exception("rate limited")

        with patch("duckduckgo_search.DDGS", return_value=mock_ddgs):
            result = await web_search.ainvoke(
                {"query": "test"},
                config=_make_config("user-1"),
            )

        assert "Search failed" in result


class TestGetAllTools:
    def test_returns_all_tools(self):
        tools = get_all_tools()
        expected_count = 11
        assert len(tools) == expected_count

    def test_tool_names(self):
        tools = get_all_tools()
        names = {t.name for t in tools}
        assert names == {
            "create_project",
            "list_projects",
            "get_project",
            "set_project_secret",
            "create_story",
            "list_stories",
            "get_story",
            "get_run_status",
            "set_reminder",
            "notify_user",
            "web_search",
        }
