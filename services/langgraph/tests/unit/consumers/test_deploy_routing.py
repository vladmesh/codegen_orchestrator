"""Unit tests for deploy success/smoke-failure result handlers.

Verifies that handlers store correct deploy_outcome in run.result
and do NOT perform story transitions (dispatcher's job).
"""

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.queues.deploy import DeployMessage, DeployOutcome, DeployTrigger

_HANDLER_PATCH = "src.consumers.deploy_result_handler"
_FAILURE_PATCH = "src.consumers.deploy_failure_handler"


def _make_deploy_msg(**overrides) -> DeployMessage:
    """Build a valid DeployMessage."""
    defaults = {
        "task_id": "deploy-test-1",
        "project_id": "proj-1",
        "user_id": "123",
        "callback_stream": "cb:123",
        "triggered_by": DeployTrigger.ENGINEERING.value,
        "action": "create",
        "story_id": "story-1",
        "deploy_fix_attempt": 0,
    }
    defaults.update(overrides)
    return DeployMessage.model_validate(defaults)


class TestHandleDeploySuccess:
    """_handle_deploy_success stores success outcome, no story transitions."""

    @pytest.mark.asyncio
    async def test_stores_success_outcome(self):
        from src.consumers.deploy_result_handler import _handle_deploy_success

        mock_redis = AsyncMock()
        project = ProjectDTO(
            id="00000000-0000-0000-0000-000000000001",
            name="test-project",
            status=ProjectStatus.ACTIVE,
            owner_id=1,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )

        with patch(f"{_HANDLER_PATCH}.api_client") as mock_api:
            mock_api.patch = AsyncMock()
            result = await _handle_deploy_success(
                result={"deployed_url": "https://example.com", "bot_username": "test_bot"},
                smoke_result=None,
                task_id="deploy-1",
                project_id="proj-1",
                project=project,
                callback_stream="cb:1",
                user_id="123",
                story_id="story-1",
                redis=mock_redis,
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
            name="test",
            status=ProjectStatus.ACTIVE,
            owner_id=1,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )

        with patch(f"{_HANDLER_PATCH}.api_client") as mock_api:
            mock_api.patch = AsyncMock()
            await _handle_deploy_success(
                result={"deployed_url": "https://example.com"},
                smoke_result=None,
                task_id="deploy-1",
                project_id="proj-1",
                project=project,
                callback_stream="cb:1",
                user_id="123",
                story_id="story-1",
                redis=mock_redis,
            )

            for call in mock_api.method_calls:
                assert "transition_story" not in str(call)

            # No QA message published
            mock_redis.publish_message.assert_not_called()


class TestHandleSmokeFailure:
    """_handle_smoke_failure classifies and stores outcome, no story transitions."""

    @pytest.mark.asyncio
    async def test_stores_classified_outcome(self):
        from src.consumers.deploy_result_handler import _handle_smoke_failure

        mock_redis = AsyncMock()
        msg = _make_deploy_msg()

        with (
            patch(f"{_HANDLER_PATCH}.api_client") as mock_api,
            patch(
                f"{_HANDLER_PATCH}._classify_deploy_failure",
                new_callable=AsyncMock,
                return_value="CODE_FIX",
            ),
        ):
            mock_api.patch = AsyncMock()
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
                user_id="123",
                story_id="story-1",
                redis=mock_redis,
                msg=msg,
            )

            patch_call = mock_api.patch.call_args
            run_result = patch_call[1]["json"]["result"]
            assert run_result["deploy_outcome"] == DeployOutcome.CODE_FIX.value

        assert result["status"] == "failed"
