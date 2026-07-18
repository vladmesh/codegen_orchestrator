"""Unit tests for QA consumer — process QAMessage, store outcome in run.result.

After #1030 decoupling: QA consumer is a pure technical worker. It updates
run.status and run.result only — no story transitions, no user notifications.
Story lifecycle is managed by the dispatcher's supervise_testing_stories().
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from shared.contracts.dto.application import ApplicationDTO
from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.server import ServerDTO
from shared.contracts.dto.story import StoryDTO
from shared.contracts.queues.qa import QAOutcome, QAServerInfo
from src.consumers.qa import (
    MAX_QA_LOOPS,
    _resolve_server_info,
    process_qa_job,
)

# Criteria with a prose line — not decidable over HTTP, so QA hands these to the
# agent on the server. Tests that want the HTTP path override this.
AGENT_CRITERIA = "- GET /health returns 200\n- GET /api/weather returns forecast"


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
        "ssh_user": "dev",
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
                name="weather-bot",
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
        mock.patch = AsyncMock(return_value={})
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
        "acceptance_criteria": AGENT_CRITERIA,
        "run_id": "qa-run-1",
        "bot_username": None,
        "qa_attempt": 0,
    }


class TestResolveServerInfo:
    @pytest.mark.asyncio
    async def test_resolves_server_info(self, mock_api_client):
        info = await _resolve_server_info(1)
        assert isinstance(info, QAServerInfo)
        assert info.server_ip == "1.2.3.4"
        assert info.ssh_user == "dev"
        assert "RSA" in info.ssh_key
        assert info.project_name == "weather_bot"
        mock_api_client.get_application.assert_called_once_with(1)
        mock_api_client.get_server.assert_awaited_once_with("vps-1")
        mock_api_client.get_server_ssh_key.assert_awaited_once_with("vps-1")

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


class TestProcessQAJobServerResolveFailure:
    @pytest.mark.asyncio
    async def test_server_resolve_failure_writes_terminal_error_result(
        self, mock_api_client, mock_redis, qa_message_data
    ):
        """If the server can't be resolved, the run is failed with a typed ERROR result.

        Otherwise the run would stay QUEUED and the story would sit in TESTING forever.
        """
        with patch("src.consumers.qa._resolve_server_info", new_callable=AsyncMock) as mock_resolve:
            mock_resolve.return_value = None
            result = await process_qa_job(qa_message_data, mock_redis)

        assert result["status"] == "error"
        # The run must be patched to FAILED with a typed QA ERROR result.
        patch_call = mock_api_client.patch.call_args
        run_data = patch_call[1]["json"]
        assert run_data["status"] == RunStatus.FAILED.value
        assert run_data["result"]["qa_outcome"] == QAOutcome.ERROR.value


class TestProcessQAJobPass:
    @pytest.mark.asyncio
    async def test_qa_pass_stores_outcome_in_run(
        self, mock_api_client, mock_redis, qa_message_data
    ):
        from src.consumers._qa_runner import QAResult

        with patch("src.consumers.qa.run_qa_on_server", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = QAResult(passed=True, checks=[], summary="All good", raw="")
            result = await process_qa_job(qa_message_data, mock_redis)

        assert result["status"] == "passed"
        # Two patch calls: RUNNING status + COMPLETED with result
        assert mock_api_client.patch.call_count == 2
        running_call = mock_api_client.patch.call_args_list[0]
        assert running_call[1]["json"]["status"] == RunStatus.RUNNING.value
        completed_call = mock_api_client.patch.call_args_list[1]
        assert completed_call[0][0] == "runs/qa-run-1"
        run_data = completed_call[1]["json"]
        assert run_data["status"] == RunStatus.COMPLETED.value
        assert run_data["result"]["qa_outcome"] == QAOutcome.PASSED.value

    @pytest.mark.asyncio
    async def test_qa_pass_does_not_transition_story(
        self, mock_api_client, mock_redis, qa_message_data
    ):
        from src.consumers._qa_runner import QAResult

        with patch("src.consumers.qa.run_qa_on_server", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = QAResult(passed=True, checks=[], summary="All good", raw="")
            await process_qa_job(qa_message_data, mock_redis)

        assert not hasattr(mock_api_client, "transition_story") or (
            not mock_api_client.transition_story.called
        )


class TestProcessQAJobFail:
    @pytest.mark.asyncio
    async def test_qa_fail_stores_failed_outcome(
        self, mock_api_client, mock_redis, qa_message_data
    ):
        from src.consumers._qa_runner import QAResult

        with patch("src.consumers.qa.run_qa_on_server", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = QAResult(
                passed=False,
                checks=[{"name": "weather endpoint", "pass": False, "detail": "404"}],
                summary="Weather endpoint broken",
                raw="",
            )
            result = await process_qa_job(qa_message_data, mock_redis)

        assert result["status"] == "qa_failed"
        call_kwargs = mock_api_client.patch.call_args
        run_data = call_kwargs[1]["json"]
        assert run_data["result"]["qa_outcome"] == QAOutcome.FAILED.value
        assert run_data["result"]["summary"] == "Weather endpoint broken"
        assert len(run_data["result"]["failed_checks"]) == 1

    @pytest.mark.asyncio
    async def test_qa_fail_does_not_transition_story(
        self, mock_api_client, mock_redis, qa_message_data
    ):
        from src.consumers._qa_runner import QAResult

        with patch("src.consumers.qa.run_qa_on_server", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = QAResult(passed=False, checks=[], summary="Broken", raw="")
            await process_qa_job(qa_message_data, mock_redis)

        assert not hasattr(mock_api_client, "transition_story") or (
            not mock_api_client.transition_story.called
        )

    @pytest.mark.asyncio
    async def test_qa_fail_does_not_create_fix_task(
        self, mock_api_client, mock_redis, qa_message_data
    ):
        """Fix task creation moved to dispatcher — QA consumer only stores result."""
        from src.consumers._qa_runner import QAResult

        with patch("src.consumers.qa.run_qa_on_server", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = QAResult(passed=False, checks=[], summary="Broken", raw="")
            await process_qa_job(qa_message_data, mock_redis)

        mock_api_client.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_max_qa_loops_stores_exhausted_outcome(
        self, mock_api_client, mock_redis, qa_message_data
    ):
        from src.consumers._qa_runner import QAResult

        qa_message_data["qa_attempt"] = MAX_QA_LOOPS

        with patch("src.consumers.qa.run_qa_on_server", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = QAResult(
                passed=False, checks=[], summary="Still broken", raw=""
            )
            result = await process_qa_job(qa_message_data, mock_redis)

        assert result["status"] == "qa_exhausted"
        call_kwargs = mock_api_client.patch.call_args
        run_data = call_kwargs[1]["json"]
        assert run_data["result"]["qa_outcome"] == QAOutcome.EXHAUSTED.value


class TestHealthOnlyCriteriaRouting:
    """Criteria that only state GET expectations are decided over HTTP, no agent."""

    @pytest.mark.asyncio
    async def test_health_only_criteria_pass_without_the_agent(
        self, mock_api_client, mock_redis, qa_message_data
    ):
        """A health-only story (the mega case) completes with outcome passed."""
        from src.consumers._qa_runner import QAResult

        qa_message_data["acceptance_criteria"] = "- GET /health returns 200"

        with (
            patch("src.consumers.qa.run_health_checks", new_callable=AsyncMock) as mock_health,
            patch("src.consumers.qa.run_qa_on_server", new_callable=AsyncMock) as mock_agent,
        ):
            mock_health.return_value = QAResult(
                passed=True,
                checks=[{"name": "GET /health returns 200", "pass": True, "detail": "got 200"}],
                summary="1 GET check(s) passed",
            )
            result = await process_qa_job(qa_message_data, mock_redis)

        assert result["status"] == "passed"
        mock_agent.assert_not_called()

        checks = mock_health.call_args[1]["checks"]
        assert [(c.path, c.expected_status) for c in checks] == [("/health", 200)]
        assert mock_health.call_args[1]["deployed_url"] == "https://weather.example.com"

        completed_call = mock_api_client.patch.call_args_list[-1]
        run_data = completed_call[1]["json"]
        assert run_data["status"] == RunStatus.COMPLETED.value
        assert run_data["result"]["qa_outcome"] == QAOutcome.PASSED.value

    @pytest.mark.asyncio
    async def test_failing_health_check_stores_failed_outcome(
        self, mock_api_client, mock_redis, qa_message_data
    ):
        from src.consumers._qa_runner import QAResult

        qa_message_data["acceptance_criteria"] = "- GET /health returns 200"

        with patch("src.consumers.qa.run_health_checks", new_callable=AsyncMock) as mock_health:
            mock_health.return_value = QAResult(
                passed=False,
                checks=[
                    {
                        "name": "GET /health returns 200",
                        "pass": False,
                        "detail": "got 502, expected 200",
                    }
                ],
                summary="1/1 GET check(s) failed",
            )
            result = await process_qa_job(qa_message_data, mock_redis)

        assert result["status"] == "qa_failed"
        run_data = mock_api_client.patch.call_args[1]["json"]
        assert run_data["result"]["qa_outcome"] == QAOutcome.FAILED.value
        assert run_data["result"]["failed_checks"][0]["detail"] == "got 502, expected 200"

    @respx.mock
    @pytest.mark.asyncio
    async def test_http_200_passes_when_the_server_cannot_be_resolved(
        self, mock_api_client, mock_redis, qa_message_data
    ):
        """An HTTP-decidable check must not fail over agent scaffolding it never uses.

        The server's SSH key is what the coding agent needs to log in. A criteria
        block of plain GET expectations is answered by the deployed URL alone, so a
        missing key must not turn a service that answers 200 into a terminal error.
        """
        route = respx.get("https://weather.example.com/health").mock(
            return_value=httpx.Response(200)
        )
        # Server resolution would fail outright: no SSH key for this application.
        mock_api_client.get_server_ssh_key.return_value = None
        qa_message_data["acceptance_criteria"] = "- GET /health returns 200"

        result = await process_qa_job(qa_message_data, mock_redis)

        assert result["status"] == "passed"
        assert route.called
        # Nothing about the server — or its private key — is read on this path.
        mock_api_client.get_application.assert_not_called()
        mock_api_client.get_server.assert_not_called()
        mock_api_client.get_server_ssh_key.assert_not_called()

        run_data = mock_api_client.patch.call_args_list[-1][1]["json"]
        assert run_data["status"] == RunStatus.COMPLETED.value
        assert run_data["result"]["qa_outcome"] == QAOutcome.PASSED.value

    @respx.mock
    @pytest.mark.asyncio
    async def test_http_200_passes_for_a_tg_bot_project_without_a_bot_username(
        self, mock_api_client, mock_redis, qa_message_data
    ):
        """bot_username is what the agent talks to Telegram with, not a GET check.

        A tg_bot project's first story carries the seeded health check, so it must
        not error out before the architect has written any Telegram criteria.
        """
        respx.get("https://weather.example.com/health").mock(return_value=httpx.Response(200))
        mock_api_client.get_project.return_value = ProjectDTO(
            id="116c9678-5872-4ce5-8332-9a267ab27604",
            name="tg-bot-project",
            status=ProjectStatus.ACTIVE,
            config={"modules": ["tg_bot"]},
            owner_id=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        qa_message_data["bot_username"] = None
        qa_message_data["acceptance_criteria"] = "- GET /health returns 200"

        result = await process_qa_job(qa_message_data, mock_redis)

        assert result["status"] == "passed"

    @pytest.mark.asyncio
    async def test_prose_criteria_still_go_to_the_agent(
        self, mock_api_client, mock_redis, qa_message_data
    ):
        """Only fully machine-checkable criteria skip the agent."""
        from src.consumers._qa_runner import QAResult

        qa_message_data["acceptance_criteria"] = AGENT_CRITERIA

        with (
            patch("src.consumers.qa.run_health_checks", new_callable=AsyncMock) as mock_health,
            patch("src.consumers.qa.run_qa_on_server", new_callable=AsyncMock) as mock_agent,
        ):
            mock_agent.return_value = QAResult(passed=True, checks=[], summary="OK", raw="")
            result = await process_qa_job(qa_message_data, mock_redis)

        assert result["status"] == "passed"
        mock_health.assert_not_called()
        mock_agent.assert_called_once()


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

    @pytest.mark.asyncio
    async def test_inflight_dedup_uses_application_id_when_no_story(
        self, mock_api_client, mock_redis
    ):
        """Standalone QA (no story_id) uses application_id for inflight dedup."""
        from src.consumers._qa_runner import QAResult

        mock_api_client.get_application.return_value = _application(id=42)

        data = {
            "story_id": "",
            "project_id": "proj-1",
            "user_id": "12345",
            "deployed_url": "https://weather.example.com",
            "application_id": 42,
            "acceptance_criteria": AGENT_CRITERIA,
            "run_id": "qa-run-1",
            "qa_attempt": 0,
        }

        with patch("src.consumers.qa.run_qa_on_server", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = QAResult(passed=True, checks=[], summary="OK", raw="")
            await process_qa_job(data, mock_redis)

        # Inflight key should use application_id, not empty story_id
        set_call = mock_redis.redis.set.call_args
        inflight_key = set_call[0][0]
        assert "42" in inflight_key
        assert inflight_key != "qa:inflight:"  # not empty

    @pytest.mark.asyncio
    async def test_qa_runs_the_criteria_from_the_message(self, mock_api_client, mock_redis):
        """QA tests against the criteria the producer resolved, not its own lookup.

        The producer resolves them from the repository before creating the run, so
        the consumer must not re-read them — that split is what lost them before.
        """
        from src.consumers._qa_runner import QAResult

        data = {
            "story_id": "",
            "project_id": "proj-1",
            "user_id": "12345",
            "deployed_url": "https://weather.example.com",
            "application_id": 1,
            "acceptance_criteria": AGENT_CRITERIA,
            "run_id": "qa-run-1",
            "qa_attempt": 0,
        }

        with patch("src.consumers.qa.run_qa_on_server", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = QAResult(passed=True, checks=[], summary="OK", raw="")
            result = await process_qa_job(data, mock_redis)

        assert result["status"] == "passed"
        assert mock_run.call_args[1]["acceptance_criteria"] == AGENT_CRITERIA
        mock_api_client.get_story.assert_not_called()

    @pytest.mark.asyncio
    async def test_bot_username_missing_for_tg_bot_stores_error(
        self, mock_api_client, mock_redis, qa_message_data
    ):
        mock_api_client.get_project.return_value = ProjectDTO(
            id="116c9678-5872-4ce5-8332-9a267ab27604",
            name="tg-bot-project",
            status=ProjectStatus.ACTIVE,
            config={"modules": ["tg_bot"]},
            owner_id=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        qa_message_data["bot_username"] = None

        result = await process_qa_job(qa_message_data, mock_redis)
        assert result["status"] == "error"
        call_kwargs = mock_api_client.patch.call_args
        run_data = call_kwargs[1]["json"]
        assert run_data["result"]["qa_outcome"] == QAOutcome.ERROR.value
