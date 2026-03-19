"""Unit tests for QA consumer — process QAMessage, route pass/fail."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.application import ApplicationDTO
from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.dto.server import ServerDTO
from shared.contracts.dto.story import StoryDTO
from shared.contracts.queues.qa import QAServerInfo
from src.consumers.qa import (
    MAX_QA_LOOPS,
    _resolve_server_info,
    process_qa_job,
)


def _application(**overrides) -> ApplicationDTO:
    base = {
        "id": 1,
        "repo_id": "repo-1",
        "server_handle": "vps-1",
        "service_name": "weather_bot",
        "status": "running",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ApplicationDTO(**base)


def _server(**overrides) -> ServerDTO:
    base = {
        "handle": "vps-1",
        "host": "vps-1.example.com",
        "public_ip": "1.2.3.4",
        "status": "active",
        "is_managed": True,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ServerDTO(**base)


def _qa_story(**overrides) -> StoryDTO:
    import uuid

    base = {
        "id": "story-1",
        "project_id": uuid.uuid4(),
        "title": "Build weather API",
        "description": "Build a weather API that returns current weather for any city",
        "type": "product",
        "status": "testing",
        "priority": 0,
        "created_by": "system",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return StoryDTO(**base)


@pytest.fixture
def mock_api_client():
    with patch("src.consumers.qa.api_client") as mock:
        mock.get_story = AsyncMock(return_value=_qa_story())
        mock.get_project = AsyncMock(
            return_value=ProjectDTO(
                id="116c9678-5872-4ce5-8332-9a267ab27604",
                name="weather_bot",
                status=ProjectStatus.ACTIVE,
                config={},
                owner_id=1,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        mock.get_application = AsyncMock(return_value=_application())
        mock.get_server = AsyncMock(return_value=_server())
        mock.get_server_ssh_key = AsyncMock(
            return_value="-----BEGIN RSA KEY-----\nfake\n-----END RSA KEY-----"
        )
        mock.transition_story = AsyncMock(return_value={})
        mock.create_task = AsyncMock(return_value={"id": "task-fix-1"})
        yield mock


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.redis = AsyncMock()
    redis.redis.set = AsyncMock(return_value=True)  # inflight marker acquired
    redis.redis.delete = AsyncMock()
    redis.publish_flat = AsyncMock()
    redis.publish_message = AsyncMock()
    return redis


@pytest.fixture
def qa_message_data():
    return {
        "story_id": "story-1",
        "project_id": "proj-1",
        "user_id": "12345",
        "deployed_url": "https://weather.example.com",
        "application_id": 1,
        "bot_username": None,
        "qa_attempt": 0,
    }


class TestResolveServerInfo:
    @pytest.mark.asyncio
    async def test_resolves_server_info(self, mock_api_client):
        info = await _resolve_server_info(1)
        assert isinstance(info, QAServerInfo)
        assert info.server_ip == "1.2.3.4"
        assert "RSA" in info.ssh_key
        assert info.project_name == "weather_bot"
        mock_api_client.get_application.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_application_not_found(self, mock_api_client):
        mock_api_client.get_application.side_effect = Exception("Not found")
        assert await _resolve_server_info(999) is None

    @pytest.mark.asyncio
    async def test_no_ssh_key_returns_none(self, mock_api_client):
        mock_api_client.get_server_ssh_key.return_value = None
        assert await _resolve_server_info(1) is None

    @pytest.mark.asyncio
    async def test_no_server_handle_returns_none(self, mock_api_client):
        mock_api_client.get_application.return_value = _application(server_handle="")
        assert await _resolve_server_info(1) is None


class TestProcessQAJobPass:
    @pytest.mark.asyncio
    async def test_qa_pass_completes_story(self, mock_api_client, mock_redis, qa_message_data):
        from src.consumers._qa_runner import QAResult

        with patch("src.consumers.qa.run_qa_on_server", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = QAResult(passed=True, checks=[], summary="All good", raw="")
            with patch("src.consumers.qa.publish_story_event", new_callable=AsyncMock):
                result = await process_qa_job(qa_message_data, mock_redis)

        assert result["status"] == "passed"
        mock_api_client.transition_story.assert_called_once_with("story-1", "complete")

    @pytest.mark.asyncio
    async def test_qa_pass_notifies_user(self, mock_api_client, mock_redis, qa_message_data):
        from src.consumers._qa_runner import QAResult

        with patch("src.consumers.qa.run_qa_on_server", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = QAResult(passed=True, checks=[], summary="All good", raw="")
            with patch(
                "src.consumers.qa.publish_story_event", new_callable=AsyncMock
            ) as mock_event:
                await process_qa_job(qa_message_data, mock_redis)

        mock_event.assert_called_once()
        call_kwargs = mock_event.call_args
        assert call_kwargs.kwargs["event"] == "story_completed"


class TestProcessQAJobFail:
    @pytest.mark.asyncio
    async def test_qa_fail_creates_fix_task(self, mock_api_client, mock_redis, qa_message_data):
        from src.consumers._qa_runner import QAResult

        with patch("src.consumers.qa.run_qa_on_server", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = QAResult(
                passed=False,
                checks=[{"name": "weather endpoint", "pass": False, "detail": "404"}],
                summary="Weather endpoint broken",
                raw="",
            )
            with patch("src.consumers.qa.publish_story_event", new_callable=AsyncMock):
                result = await process_qa_job(qa_message_data, mock_redis)

        assert result["status"] == "qa_failed"
        mock_api_client.create_task.assert_called_once()
        task_data = mock_api_client.create_task.call_args[0][0]
        assert task_data["story_id"] == "story-1"
        assert "weather" in task_data["description"].lower() or "404" in task_data["description"]

    @pytest.mark.asyncio
    async def test_qa_fail_creates_task_with_todo_status(
        self, mock_api_client, mock_redis, qa_message_data
    ):
        from src.consumers._qa_runner import QAResult

        with patch("src.consumers.qa.run_qa_on_server", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = QAResult(
                passed=False,
                checks=[{"name": "health", "pass": False, "detail": "503"}],
                summary="Service unhealthy",
                raw="",
            )
            with patch("src.consumers.qa.publish_story_event", new_callable=AsyncMock):
                await process_qa_job(qa_message_data, mock_redis)

        task_data = mock_api_client.create_task.call_args[0][0]
        assert task_data["status"] == "todo"

    @pytest.mark.asyncio
    async def test_qa_fail_rolls_back_story(self, mock_api_client, mock_redis, qa_message_data):
        from src.consumers._qa_runner import QAResult

        with patch("src.consumers.qa.run_qa_on_server", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = QAResult(passed=False, checks=[], summary="Broken", raw="")
            with patch("src.consumers.qa.publish_story_event", new_callable=AsyncMock):
                await process_qa_job(qa_message_data, mock_redis)

        mock_api_client.transition_story.assert_called_once_with("story-1", "start")

    @pytest.mark.asyncio
    async def test_max_qa_loops_fails_story(self, mock_api_client, mock_redis, qa_message_data):
        from src.consumers._qa_runner import QAResult

        qa_message_data["qa_attempt"] = MAX_QA_LOOPS  # at limit

        with patch("src.consumers.qa.run_qa_on_server", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = QAResult(
                passed=False, checks=[], summary="Still broken", raw=""
            )
            with patch("src.consumers.qa.publish_story_event", new_callable=AsyncMock):
                result = await process_qa_job(qa_message_data, mock_redis)

        assert result["status"] == "qa_exhausted"
        mock_api_client.transition_story.assert_called_once_with("story-1", "fail")
        mock_api_client.create_task.assert_not_called()


class TestProcessQAJobEdgeCases:
    @pytest.mark.asyncio
    async def test_application_not_found(self, mock_api_client, mock_redis, qa_message_data):
        mock_api_client.get_application.side_effect = Exception("Not found")

        result = await process_qa_job(qa_message_data, mock_redis)
        assert result["status"] == "error"
        assert "application" in result.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_no_ssh_key_errors(self, mock_api_client, mock_redis, qa_message_data):
        mock_api_client.get_server_ssh_key.return_value = None

        result = await process_qa_job(qa_message_data, mock_redis)
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_inflight_dedup_skips(self, mock_api_client, mock_redis, qa_message_data):
        mock_redis.redis.set.return_value = False  # already inflight

        result = await process_qa_job(qa_message_data, mock_redis)
        assert result["status"] == "skipped"
