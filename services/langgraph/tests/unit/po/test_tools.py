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
    reopen_story,
    set_bot_access,
    set_project_secret,
    set_reminder,
    teardown_project,
    validate_telegram_token,
    web_search,
)

BOT_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"  # noqa: S105


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
    resp.is_success = 200 <= status_code < 300
    resp.json.return_value = data
    return resp


def _make_config(user_id: str = "test-user", retry_story_id: str = "") -> dict:
    """Create a RunnableConfig with user_id."""
    configurable = {"thread_id": f"po-user-{user_id}", "user_id": user_id}
    if retry_story_id:
        configurable["retry_story_id"] = retry_story_id
    return {"configurable": configurable}


class TestCreateProject:
    @pytest.mark.asyncio
    async def test_creates_project_with_modules(self, mock_api_client):
        project_data = {"id": "abc123", "title": "My Bot", "slug": "my-bot-abc1"}
        mock_api_client.post.return_value = _make_response(project_data)

        result = await create_project.ainvoke(
            {"title": "My Bot", "modules": "backend,tg_bot", "description": "A test bot"},
            config=_make_config("user-42"),
        )

        # create_project makes 2 POST calls: project + repository
        assert mock_api_client.post.call_count == 2
        project_call = mock_api_client.post.call_args_list[0]
        assert project_call[0][0] == "/api/projects/"
        payload = project_call[1]["json"]
        assert payload["title"] == "My Bot"
        assert "backend" in payload["config"]["modules"]
        assert "tg_bot" in payload["config"]["modules"]
        assert "Project created" in result
        assert "abc123" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent_type", ["claude", "factory", "codex"])
    async def test_persists_selected_developer_agent(self, mock_api_client, agent_type):
        mock_api_client.post.return_value = _make_response(
            {"id": "x", "title": "Project", "slug": "project-1234"}
        )

        await create_project.ainvoke(
            {"title": "Project", "modules": "backend", "agent_type": agent_type},
            config=_make_config("user-1"),
        )

        project_call = mock_api_client.post.call_args_list[0]
        assert project_call[1]["json"]["config"]["agent_type"] == agent_type

    @pytest.mark.asyncio
    async def test_rejects_unknown_developer_agent(self, mock_api_client):
        result = await create_project.ainvoke(
            {"title": "Project", "modules": "backend", "agent_type": "mystery"},
            config=_make_config("user-1"),
        )

        assert result == "Error: invalid agent_type: mystery. Available: claude, codex, factory"
        mock_api_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_passes_telegram_id_header(self, mock_api_client):
        mock_api_client.post.return_value = _make_response(
            {"id": "x", "title": "Test", "slug": "test-1234"}
        )

        await create_project.ainvoke(
            {"title": "Test", "modules": "backend"},
            config=_make_config("12345"),
        )

        call_args = mock_api_client.post.call_args
        headers = call_args[1].get("headers", {})
        assert headers.get("X-Telegram-ID") == "12345"

    @pytest.mark.asyncio
    async def test_ensures_backend_module(self, mock_api_client):
        mock_api_client.post.return_value = _make_response(
            {"id": "x", "title": "Test", "slug": "test-1234"}
        )

        await create_project.ainvoke(
            {"title": "Test", "modules": "tg_bot"},
            config=_make_config("user-1"),
        )

        project_call = mock_api_client.post.call_args_list[0]
        payload = project_call[1]["json"]
        assert "backend" in payload["config"]["modules"]

    @pytest.mark.asyncio
    async def test_rejects_invalid_modules(self, mock_api_client):
        result = await create_project.ainvoke(
            {"title": "Test", "modules": "invalid_mod"},
            config=_make_config("user-1"),
        )
        assert "Error" in result
        assert "invalid_mod" in result
        mock_api_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_repository_failure_propagates(self, mock_api_client):
        mock_api_client.post.side_effect = [
            _make_response({"id": "abc123", "title": "My Bot", "slug": "my-bot-abc1"}),
            httpx.HTTPStatusError(
                "repository unavailable", request=MagicMock(), response=MagicMock()
            ),
        ]

        with pytest.raises(httpx.HTTPStatusError, match="repository unavailable"):
            await create_project.ainvoke(
                {"title": "My Bot", "modules": "backend"},
                config=_make_config(),
            )


class TestListProjects:
    @pytest.mark.asyncio
    async def test_lists_projects(self, mock_api_client):
        mock_api_client.get.return_value = _make_response(
            [
                {"id": "1", "title": "Project A", "slug": "proj-a-1111", "status": "active"},
                {"id": "2", "title": "Project B", "slug": "proj-b-2222", "status": "draft"},
            ]
        )

        result = await list_projects.ainvoke({}, config=_make_config("user-42"))

        assert "Project A" in result
        assert "Project B" in result
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
        project = {"id": "abc", "title": "My Bot", "slug": "my-bot-abc1", "status": "active"}
        mock_api_client.get.return_value = _make_response(project)

        result = await get_project.ainvoke({"project_id": "abc"}, config=_make_config("user-42"))

        parsed = json.loads(result)
        assert parsed["title"] == "My Bot"
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
        mock_api_client.post.return_value = _make_response({"keys": ["OPENROUTER_API_KEY"]})

        result = await set_project_secret.ainvoke(
            {"project_id": "abc", "key": "OPENROUTER_API_KEY", "value": "sk-or-123"},
            config=_make_config("user-42"),
        )

        assert "Secret" in result
        call_args = mock_api_client.post.call_args
        assert call_args[0][0] == "/api/projects/abc/config/secrets"
        payload = call_args[1]["json"]
        assert payload["secrets"]["OPENROUTER_API_KEY"] == "sk-or-123"

    @pytest.mark.asyncio
    async def test_server_refusal_comes_back_as_an_error_message(self, mock_api_client):
        """A bot token is refused server-side — the PO must see why, not a traceback."""
        mock_api_client.post.return_value = _make_response(
            {"detail": "TELEGRAM_BOT_TOKEN cannot be set directly — use /telegram/token"},
            status_code=422,
        )

        result = await set_project_secret.ainvoke(
            {"project_id": "abc", "key": "TELEGRAM_BOT_TOKEN", "value": BOT_TOKEN},
            config=_make_config("user-42"),
        )

        assert result.startswith("Error:")
        assert "/telegram/token" in result

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
    async def test_legacy_bot_access_secret_is_refused(self, mock_api_client):
        """The PO cannot create a bot audience through the legacy secret path."""
        result = await set_project_secret.ainvoke(
            {
                "project_id": "abc",
                "key": "ADMIN_TELEGRAM_ID",
                "value": "42",
                "hint": "Telegram ID of the bot admin",
            },
            config=_make_config("user-42"),
        )

        assert result.startswith("Error:")
        assert "set_bot_access" in result
        mock_api_client.post.assert_not_called()

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


class TestBotAccess:
    @pytest.mark.asyncio
    async def test_only_me_stores_the_callers_contract_audience(self, mock_api_client):
        mock_api_client.post.return_value = _make_response({"mode": "only_me"})

        result = await set_bot_access.ainvoke(
            {"project_id": "abc", "mode": "only_me"}, config=_make_config("77777")
        )

        assert "only_me" in result
        assert mock_api_client.post.call_args.args[0] == "/api/projects/abc/config/bot-access"
        assert mock_api_client.post.call_args.kwargs["json"] == {
            "mode": "only_me",
            "allowed_telegram_ids": "77777",
        }

    @pytest.mark.asyncio
    async def test_public_stores_an_explicit_empty_audience(self, mock_api_client):
        mock_api_client.post.return_value = _make_response({"mode": "public"})

        await set_bot_access.ainvoke(
            {"project_id": "abc", "mode": "public"}, config=_make_config("77777")
        )

        assert mock_api_client.post.call_args.kwargs["json"] == {
            "mode": "public",
            "allowed_telegram_ids": "",
        }

    async def test_custom_stores_the_selected_contract_audience(self, mock_api_client):
        mock_api_client.post.return_value = _make_response({"mode": "custom"})

        result = await set_bot_access.ainvoke(
            {"project_id": "abc", "mode": "custom", "allowed_telegram_ids": "77777,88888"},
            config=_make_config("77777"),
        )

        assert "custom" in result
        assert mock_api_client.post.call_args.kwargs["json"] == {
            "mode": "custom",
            "allowed_telegram_ids": "77777,88888",
        }

    @pytest.mark.asyncio
    async def test_custom_requires_a_base_audience(self, mock_api_client):
        result = await set_bot_access.ainvoke(
            {"project_id": "abc", "mode": "custom"}, config=_make_config("77777")
        )

        assert result.startswith("Error:")
        mock_api_client.post.assert_not_called()


class TestValidateTelegramToken:
    """The tool is a thin mouth for the server verdict — no validation of its own."""

    @pytest.mark.asyncio
    async def test_posts_token_to_the_validator_endpoint(self, mock_api_client):
        mock_api_client.post.return_value = _make_response(
            {
                "status": "ok",
                "reason_code": None,
                "user_message": "Token is valid. Bot: @palindrome_bot",
                "bot_username": "palindrome_bot",
                "checks": [{"name": "format", "passed": True, "reason_code": None, "detail": ""}],
            }
        )

        result = await validate_telegram_token.ainvoke(
            {"project_id": "abc", "token": BOT_TOKEN},
            config=_make_config("user-42"),
        )

        call_args = mock_api_client.post.call_args
        assert call_args[0][0] == "/api/projects/abc/telegram/token"
        assert call_args[1]["json"] == {"token": BOT_TOKEN}
        assert call_args[1]["headers"]["X-Telegram-ID"] == "user-42"
        assert "palindrome_bot" in result
        # No side path: the tool never touches secrets or repositories itself.
        mock_api_client.patch.assert_not_called()
        assert mock_api_client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_rejected_verdict_is_relayed_with_reason_code(self, mock_api_client):
        mock_api_client.post.return_value = _make_response(
            {
                "status": "rejected",
                "reason_code": "invalid_token",
                "user_message": "Telegram rejected this token: Unauthorized.",
                "bot_username": None,
                "checks": [],
            }
        )

        result = await validate_telegram_token.ainvoke(
            {"project_id": "abc", "token": "nope"},
            config=_make_config("user-42"),
        )

        assert "rejected" in result.lower()
        assert "invalid_token" in result
        assert "Unauthorized" in result

    @pytest.mark.asyncio
    async def test_own_project_conflict_names_the_holder_and_the_way_out(self, mock_api_client):
        """A dead end for the user unless the tool passes the holder id to the agent."""
        mock_api_client.post.return_value = _make_response(
            {
                "status": "rejected",
                "reason_code": "bound_to_own_project",
                "user_message": 'This bot is already connected to your project "Palindrome".',
                "bot_username": None,
                "conflict_project_id": "11111111-1111-1111-1111-111111111111",
                "checks": [],
            }
        )

        result = await validate_telegram_token.ainvoke(
            {"project_id": "abc", "token": BOT_TOKEN},
            config=_make_config("user-42"),
        )

        assert "11111111-1111-1111-1111-111111111111" in result
        assert "teardown_project" in result

    @pytest.mark.asyncio
    async def test_foreign_holder_is_not_offered_a_teardown(self, mock_api_client):
        """Nothing to free: the holding project is not the user's to tear down."""
        mock_api_client.post.return_value = _make_response(
            {
                "status": "rejected",
                "reason_code": "bound_elsewhere",
                "user_message": "This bot is already in use.",
                "bot_username": None,
                "conflict_project_id": None,
                "checks": [],
            }
        )

        result = await validate_telegram_token.ainvoke(
            {"project_id": "abc", "token": BOT_TOKEN},
            config=_make_config("user-42"),
        )

        assert "teardown_project" not in result


PROJECT_ID = "11111111-1111-1111-1111-111111111111"


def _teardown_state(status: str, **overrides) -> dict:
    state = {
        "project_id": PROJECT_ID,
        "status": status,
        "project_status": "archived" if status == "completed" else "active",
        "pending_application_ids": [],
        "released_bot_username": None,
        "error": None,
    }
    state.update(overrides)
    return state


class TestTeardownProject:
    """Teardown runs on the API under the user's identity, and reports what it freed."""

    @pytest.fixture(autouse=True)
    def _no_waiting(self):
        """The wait is real in production and pointless in a test."""
        with patch("src.agents.po.tools_projects.TEARDOWN_POLL_INTERVAL_SECONDS", 0):
            yield

    @pytest.mark.asyncio
    async def test_calls_the_teardown_endpoint_as_the_user(self, mock_api_client):
        mock_api_client.post.return_value = _make_response(
            _teardown_state("completed", released_bot_username="palindrome_bot")
        )

        result = await teardown_project.ainvoke(
            {"project_id": PROJECT_ID},
            config=_make_config("user-42"),
        )

        call_args = mock_api_client.post.call_args
        assert call_args[0][0] == f"/api/projects/{PROJECT_ID}/teardown"
        assert call_args[1]["headers"]["X-Telegram-ID"] == "user-42"
        assert "palindrome_bot" in result
        # The API owns the teardown: no direct undeploy or archive from the tool.
        mock_api_client.patch.assert_not_called()
        assert mock_api_client.post.call_count == 1
        # Nothing was pending, so there was nothing to wait for.
        mock_api_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_waits_for_the_application_to_be_down_before_calling_the_bot_free(
        self, mock_api_client
    ):
        """The bot polls its token until the containers stop — rebinding before that fails."""
        mock_api_client.post.return_value = _make_response(
            _teardown_state("pending", pending_application_ids=[7])
        )
        mock_api_client.get.side_effect = [
            _make_response(_teardown_state("pending", pending_application_ids=[7])),
            _make_response(_teardown_state("completed", released_bot_username="palindrome_bot")),
        ]

        result = await teardown_project.ainvoke(
            {"project_id": PROJECT_ID},
            config=_make_config("user-42"),
        )

        assert mock_api_client.get.call_count == 2
        assert mock_api_client.get.call_args[0][0] == f"/api/projects/{PROJECT_ID}/teardown"
        assert "down and archived" in result
        assert "palindrome_bot" in result

    @pytest.mark.asyncio
    async def test_a_teardown_that_never_finishes_does_not_free_the_token(self, mock_api_client):
        """Timing out is not permission to rebind: the agent is told to come back later."""
        mock_api_client.post.return_value = _make_response(
            _teardown_state("pending", pending_application_ids=[7])
        )
        mock_api_client.get.return_value = _make_response(
            _teardown_state("pending", pending_application_ids=[7])
        )

        with patch("src.agents.po.tools_projects.TEARDOWN_TIMEOUT_SECONDS", 0):
            result = await teardown_project.ainvoke(
                {"project_id": PROJECT_ID},
                config=_make_config("user-42"),
            )

        assert "still shutting down" in result
        assert "cannot be used elsewhere yet" in result

    @pytest.mark.asyncio
    async def test_a_failed_undeploy_is_reported_as_a_failure(self, mock_api_client):
        mock_api_client.post.return_value = _make_response(
            _teardown_state("failed", pending_application_ids=[7], error="SSH command failed")
        )

        result = await teardown_project.ainvoke(
            {"project_id": PROJECT_ID},
            config=_make_config("user-42"),
        )

        assert "failed" in result
        assert "SSH command failed" in result
        assert "do not reuse the token" in result

    @pytest.mark.asyncio
    async def test_someone_elses_project_comes_back_as_a_message(self, mock_api_client):
        """A 403 is an answer for the user, not a crash to retry through."""
        mock_api_client.post.return_value = _make_response(
            {"detail": "Access denied: not project owner"}, status_code=403
        )

        result = await teardown_project.ainvoke(
            {"project_id": "11111111-1111-1111-1111-111111111111"},
            config=_make_config("user-42"),
        )

        assert result.startswith("Error:")
        assert "not project owner" in result
        mock_api_client.post.return_value.raise_for_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_project_with_no_bot_says_so(self, mock_api_client):
        mock_api_client.post.return_value = _make_response(_teardown_state("completed"))

        result = await teardown_project.ainvoke(
            {"project_id": PROJECT_ID},
            config=_make_config("user-42"),
        )

        assert "holds no bot any more" in result


class TestCreateStory:
    @pytest.mark.asyncio
    async def test_third_matching_qa_failure_reminder_blocks_new_story(
        self, mock_api_client, mock_stream_client
    ):
        """Reminder provenance blocks the third story in the same QA failure chain."""
        mock_api_client.get.side_effect = [
            _make_response({"id": "abc", "status": "active", "config": {}}),
            _make_response(
                [
                    {
                        "id": "story-first",
                        "status": "failed",
                    },
                    {
                        "id": "story-held",
                        "status": "waiting_human_review",
                        "quarantine_reason": {
                            "qa_outcome": "failed",
                            "qa_failure": {
                                "fingerprint": "a1b2c3d4",
                                "fingerprint_attempt": 3,
                            },
                        },
                    },
                ]
            ),
        ]

        result = await create_story.ainvoke(
            {
                "project_id": "abc",
                "title": "Try the fix again",
                "description": "Retry the same failing feature",
            },
            config=_make_config("user-42", retry_story_id="story-held"),
        )

        assert "No story was created" in result
        assert "human review" in result.lower()
        mock_api_client.post.assert_not_called()
        mock_stream_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_qa_failure_hold_allows_unrelated_story(
        self, mock_api_client, mock_stream_client
    ):
        """A held retry chain must not freeze unrelated project work."""
        mock_api_client.get.side_effect = [
            _make_response({"id": "abc", "status": "active", "config": {}}),
            _make_response(
                [
                    {
                        "id": "story-held",
                        "status": "waiting_human_review",
                        "quarantine_reason": {
                            "qa_outcome": "failed",
                            "qa_failure": {"fingerprint": "a1b2c3d4"},
                        },
                    }
                ]
            ),
        ]
        mock_api_client.post.return_value = _make_response({"id": "story-export"})

        result = await create_story.ainvoke(
            {
                "project_id": "abc",
                "title": "Add export",
                "description": "Export project data as CSV",
            },
            config=_make_config("user-42"),
        )

        assert "Story created" in result
        assert mock_api_client.post.call_args.kwargs["json"]["parent_story_id"] is None
        mock_stream_client.publish_message.assert_awaited_once()

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
    async def test_spec_persistence_failure_does_not_publish_to_architect(
        self, mock_api_client, mock_stream_client
    ):
        mock_api_client.post.return_value = _make_response({"id": "story-xxx"})
        mock_api_client.get.side_effect = [
            _make_response({"id": "abc", "status": "draft", "config": {}}),
            _make_response([]),
        ]
        mock_api_client.patch.side_effect = httpx.HTTPStatusError(
            "spec persistence unavailable", request=MagicMock(), response=MagicMock()
        )

        with pytest.raises(httpx.HTTPStatusError, match="spec persistence unavailable"):
            await create_story.ainvoke(
                {
                    "project_id": "abc",
                    "title": "Create new bot",
                    "description": "Build a recipe bot",
                },
                config=_make_config(),
            )

        mock_stream_client.publish_message.assert_not_called()

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
        mock_api_client.patch.return_value = _make_response({"id": "abc"})
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
    async def test_gets_story_with_tasks_and_runs(self, mock_api_client):
        story = {"id": "s1", "title": "My story", "status": "in_progress"}
        tasks = [
            {"id": "eng-123", "status": "completed", "type": "engineering"},
            {"id": "eng-456", "status": "running", "type": "engineering"},
        ]
        runs_for_task1 = [
            {
                "id": "run-1",
                "status": "completed",
                "type": "engineering",
                "error_message": None,
                "started_at": "2026-01-01T00:00:00",
                "completed_at": "2026-01-01T00:10:00",
            },
        ]
        runs_for_task2 = [
            {
                "id": "run-2",
                "status": "running",
                "type": "engineering",
                "error_message": None,
                "started_at": "2026-01-01T00:05:00",
                "completed_at": None,
            },
        ]
        mock_api_client.get.side_effect = [
            _make_response(story),
            _make_response(tasks),
            _make_response(runs_for_task1),
            _make_response(runs_for_task2),
        ]

        result = await get_story.ainvoke({"story_id": "s1"}, config=_make_config("user-42"))

        parsed = json.loads(result)
        assert parsed["story"]["title"] == "My story"
        assert len(parsed["tasks"]) == 2
        assert parsed["tasks"][0]["runs"][0]["id"] == "run-1"
        assert parsed["tasks"][1]["runs"][0]["id"] == "run-2"

        # Verify correct API calls
        calls = mock_api_client.get.call_args_list
        assert "/api/stories/s1" in calls[0][0][0]
        assert "story_id=s1" in calls[1][0][0]
        assert "task_id=eng-123" in calls[2][0][0]
        assert "task_id=eng-456" in calls[3][0][0]


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
            {"delay_minutes": 5, "reason": "check story story-second"},
            config=_make_config("user-777"),
        )

        reminder_json = list(mock_stream_client.redis.zadd.call_args[0][1].keys())[0]
        import json

        reminder = json.loads(reminder_json)
        assert reminder["user_id"] == "user-777"
        assert reminder["story_id"] == "story-second"


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

        with patch("ddgs.DDGS", return_value=mock_ddgs):
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

        with patch("ddgs.DDGS", return_value=mock_ddgs):
            await web_search.ainvoke(
                {"query": "test", "max_results": 3},
                config=_make_config("user-1"),
            )

        mock_ddgs.text.assert_called_once_with("test", max_results=3)

    @pytest.mark.asyncio
    async def test_no_results(self):
        mock_ddgs = MagicMock()
        mock_ddgs.text.return_value = []

        with patch("ddgs.DDGS", return_value=mock_ddgs):
            result = await web_search.ainvoke(
                {"query": "nonexistent thing xyz"},
                config=_make_config("user-1"),
            )

        assert "No results" in result

    @pytest.mark.asyncio
    async def test_handles_search_error(self):
        mock_ddgs = MagicMock()
        mock_ddgs.text.side_effect = Exception("rate limited")

        with patch("ddgs.DDGS", return_value=mock_ddgs):
            result = await web_search.ainvoke(
                {"query": "test"},
                config=_make_config("user-1"),
            )

        assert "Search failed" in result


class TestReopenStory:
    @pytest.mark.asyncio
    async def test_reopens_and_publishes_architect_message(
        self, mock_api_client, mock_stream_client
    ):
        """reopen_story calls API + publishes ArchitectMessage with is_reopen=True."""
        story_data = {
            "id": "story-abc",
            "title": "Fix images",
            "project_id": "proj-1",
            "status": "in_progress",
            "user_report": "Images broken on mobile",
        }
        mock_api_client.post.return_value = _make_response(story_data)

        result = await reopen_story.ainvoke(
            {"story_id": "story-abc", "user_report": "Images broken on mobile"},
            config=_make_config("user-42"),
        )

        assert "reopened" in result.lower()
        assert "story-abc" in result

        # Verify API call
        api_call = mock_api_client.post.call_args
        assert api_call[0][0] == "/api/stories/story-abc/reopen"
        assert api_call[1]["json"]["user_report"] == "Images broken on mobile"

        # Verify ArchitectMessage
        from shared.contracts.queues.architect import ArchitectMessage
        from shared.queues import ARCHITECT_QUEUE

        pub_call = mock_stream_client.publish_message.call_args
        assert pub_call[0][0] == ARCHITECT_QUEUE
        arch_msg = pub_call[0][1]
        assert isinstance(arch_msg, ArchitectMessage)
        assert arch_msg.story_id == "story-abc"
        assert arch_msg.is_reopen is True
        assert arch_msg.user_report == "Images broken on mobile"


class TestGetAllTools:
    def test_returns_all_tools(self):
        tools = get_all_tools()
        expected_count = 15
        assert len(tools) == expected_count

    def test_tool_names(self):
        tools = get_all_tools()
        names = {t.name for t in tools}
        assert names == {
            "create_project",
            "list_projects",
            "get_project",
            "set_bot_access",
            "set_project_secret",
            "validate_telegram_token",
            "teardown_project",
            "create_story",
            "list_stories",
            "reopen_story",
            "get_story",
            "get_run_status",
            "set_reminder",
            "notify_user",
            "web_search",
        }
