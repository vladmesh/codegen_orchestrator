"""Unit tests for deploy success/smoke-failure result handlers.

Verifies that handlers store correct deploy_outcome in run.result
and do NOT perform story transitions (dispatcher's job).
"""

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.queues.deploy import DeployMessage, DeployOutcome, DeployTrigger
from tests.unit.deploy_fences import held_deploy_fence

_HANDLER_PATCH = "src.consumers.deploy_result_handler"
_FAILURE_PATCH = "src.consumers.deploy_failure_handler"


def _make_deploy_msg(**overrides) -> DeployMessage:
    """Build a valid DeployMessage."""
    defaults = {
        "task_id": "deploy-test-1",
        "project_id": "proj-1",
        "telegram_chat_id": "123",
        "callback_stream": "cb:123",
        "triggered_by": DeployTrigger.ENGINEERING.value,
        "action": "create",
        "story_id": "story-1",
        "deploy_fix_attempt": 0,
    }
    defaults.update(overrides)
    return DeployMessage.model_validate(defaults)


def _project() -> ProjectDTO:
    return ProjectDTO(
        id="00000000-0000-0000-0000-000000000001",
        initiating_run_id="test-run-1",
        title="test-project",
        slug="test-project-0000",
        status=ProjectStatus.ACTIVE,
        owner_id=1,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


class TestHandleDeploySuccess:
    """_handle_deploy_success stores success outcome, no story transitions."""

    @pytest.mark.asyncio
    async def test_stores_success_outcome(self):
        from src.consumers.deploy_result_handler import _handle_deploy_success

        mock_redis = AsyncMock()
        project = ProjectDTO(
            id="00000000-0000-0000-0000-000000000001",
            initiating_run_id="test-run-1",
            title="test-project",
            slug="test-project-0000",
            status=ProjectStatus.ACTIVE,
            owner_id=1,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )

        with patch(f"{_HANDLER_PATCH}.api_client") as mock_api:
            mock_api.patch = AsyncMock()
            # This story has no Product Brief, so nothing is seeded.
            mock_api.get_product_brief_by_story = AsyncMock(return_value=None)
            result = await _handle_deploy_success(
                result={"deployed_url": "https://example.com", "bot_username": "test_bot"},
                smoke_result=None,
                task_id="deploy-1",
                project_id="proj-1",
                project=project,
                callback_stream="cb:1",
                telegram_chat_id="123",
                story_id="story-1",
                redis=mock_redis,
                fence=held_deploy_fence(),
                msg=_make_deploy_msg(),
                application_id=42,
            )

            # Verify run was patched with success outcome
            patch_call = mock_api.patch.call_args
            run_result = patch_call[1]["json"]["result"]
            assert run_result["deploy_outcome"] == DeployOutcome.SUCCESS.value
            assert run_result["deployed_url"] == "https://example.com"
            assert run_result["application_id"] == 42
            assert run_result["bot_username"] == "test_bot"

        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_no_story_transitions(self):
        """Success handler must NOT call transition_story or publish QA."""
        from src.consumers.deploy_result_handler import _handle_deploy_success

        mock_redis = AsyncMock()
        project = ProjectDTO(
            id="00000000-0000-0000-0000-000000000001",
            initiating_run_id="test-run-1",
            title="test",
            slug="test-0000",
            status=ProjectStatus.ACTIVE,
            owner_id=1,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )

        with patch(f"{_HANDLER_PATCH}.api_client") as mock_api:
            mock_api.patch = AsyncMock()
            # This story has no Product Brief, so nothing is seeded.
            mock_api.get_product_brief_by_story = AsyncMock(return_value=None)
            await _handle_deploy_success(
                result={"deployed_url": "https://example.com"},
                smoke_result=None,
                task_id="deploy-1",
                project_id="proj-1",
                project=project,
                callback_stream="cb:1",
                telegram_chat_id="123",
                story_id="story-1",
                redis=mock_redis,
                fence=held_deploy_fence(),
                msg=_make_deploy_msg(),
            )

            for call in mock_api.method_calls:
                assert "transition_story" not in str(call)

            # No QA message published
            mock_redis.publish_message.assert_not_called()


class TestHandleSmokeFailure:
    """_handle_smoke_failure stores a retry outcome, no story transitions."""

    @pytest.mark.asyncio
    async def test_stores_retry_outcome(self):
        from src.consumers.deploy_result_handler import _handle_smoke_failure

        mock_redis = AsyncMock()
        msg = _make_deploy_msg()

        with patch(f"{_HANDLER_PATCH}.api_client") as mock_api:
            mock_api.patch = AsyncMock()
            # This story has no Product Brief, so nothing is seeded.
            mock_api.get_product_brief_by_story = AsyncMock(return_value=None)
            result = await _handle_smoke_failure(
                result={"deployed_url": "https://example.com"},
                smoke_result={
                    "status": "fail",
                    "checks": [{"module": "http", "detail": "500", "result": "fail"}],
                },
                task_id="deploy-1",
                project_id="proj-1",
                project_name="test",
                callback_stream="cb:1",
                telegram_chat_id="123",
                story_id="story-1",
                redis=mock_redis,
                fence=held_deploy_fence(),
                msg=msg,
            )

            patch_call = mock_api.patch.call_args
            run_result = patch_call[1]["json"]["result"]
            assert run_result["deploy_outcome"] == DeployOutcome.RETRY.value

        assert result["status"] == "failed"


class TestStorylessDeploySeeding:
    """A deploy that names no story seeds the project's latest confirmed brief."""

    @pytest.mark.asyncio
    async def test_an_ordinary_story_deploy_still_seeds_the_confirmed_brief(self):
        """The skip is the operation's, not every deploy that names no story."""
        from src.consumers.deploy_result_handler import _handle_deploy_success

        with (
            patch(f"{_HANDLER_PATCH}.api_client") as mock_api,
            patch(f"{_HANDLER_PATCH}.GeneratedServiceSettingsClient") as settings_client,
        ):
            mock_api.patch = AsyncMock()
            mock_api.get_project_initial_settings_brief = AsyncMock(return_value=None)
            result = await _handle_deploy_success(
                result={"deployed_url": "https://exact.example.com"},
                smoke_result=None,
                task_id="deploy-grant-1",
                project_id="proj-1",
                project=_project(),
                callback_stream="cb:1",
                telegram_chat_id="123",
                story_id="",
                redis=AsyncMock(),
                fence=held_deploy_fence(),
                msg=_make_deploy_msg(story_id=""),
                application_id=42,
            )

        assert result["status"] == "success"
        mock_api.get_project_initial_settings_brief.assert_awaited_once_with("proj-1")
        settings_client.assert_not_called()
