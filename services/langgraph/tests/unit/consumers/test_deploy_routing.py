"""Unit tests for deploy failure routing (three-way classification → correct handler)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.queues.deploy import DeployMessage, DeployTrigger

_PATCH = "src.consumers.deploy_failure_handler"


def _make_deploy_msg(**overrides) -> dict:
    """Build a valid DeployMessage dict."""
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
    return defaults


class TestDeployFailureRouting:
    """Tests for _route_deploy_failure() three-way routing."""

    @pytest.mark.asyncio
    async def test_code_fix_dispatches_to_engineering(self):
        """CODE_FIX classification should call _redispatch_to_engineering."""
        from src.consumers.deploy_failure_handler import _route_deploy_failure

        redis = AsyncMock()
        msg = DeployMessage.model_validate(_make_deploy_msg())

        with patch(f"{_PATCH}._redispatch_to_engineering", new_callable=AsyncMock) as mock_rd:
            mock_rd.return_value = True
            await _route_deploy_failure(
                classification="CODE_FIX",
                redis=redis,
                msg=msg,
                error_details="ImportError: no module named foo",
                story_id="story-1",
            )
            mock_rd.assert_called_once()

    @pytest.mark.asyncio
    async def test_retry_does_not_dispatch_to_engineering(self):
        """RETRY classification should NOT dispatch to engineering."""
        from src.consumers.deploy_failure_handler import _route_deploy_failure

        redis = AsyncMock()
        msg = DeployMessage.model_validate(_make_deploy_msg())

        with patch(f"{_PATCH}._redispatch_to_engineering", new_callable=AsyncMock) as mock_rd:
            await _route_deploy_failure(
                classification="RETRY",
                redis=redis,
                msg=msg,
                error_details="SSH timeout",
                story_id="story-1",
            )
            mock_rd.assert_not_called()

    @pytest.mark.asyncio
    async def test_give_up_calls_handle_give_up(self):
        """GIVE_UP classification should call _handle_give_up."""
        from src.consumers.deploy_failure_handler import _route_deploy_failure

        redis = AsyncMock()
        msg = DeployMessage.model_validate(_make_deploy_msg())

        with (
            patch(f"{_PATCH}._handle_give_up", new_callable=AsyncMock) as mock_gu,
            patch(f"{_PATCH}._redispatch_to_engineering", new_callable=AsyncMock) as mock_rd,
        ):
            await _route_deploy_failure(
                classification="GIVE_UP",
                redis=redis,
                msg=msg,
                error_details="port is already allocated",
                story_id="story-1",
            )
            mock_gu.assert_called_once()
            mock_rd.assert_not_called()


class TestHandleGiveUp:
    """Tests for _handle_give_up() — terminal failure, admin notified."""

    @pytest.mark.asyncio
    async def test_story_transitioned_to_failed(self):
        """GIVE_UP should transition story to failed."""
        from src.consumers.deploy_failure_handler import _handle_give_up

        redis = MagicMock()
        redis.redis = AsyncMock()

        with (
            patch(f"{_PATCH}._transition_story_safe", new_callable=AsyncMock) as mock_ts,
            patch(f"{_PATCH}.notify_admins", new_callable=AsyncMock),
            patch(
                f"{_PATCH}.get_story_worker",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await _handle_give_up(
                story_id="story-1",
                task_id="deploy-1",
                project_id="proj-1",
                error_details="port already allocated",
                redis=redis,
            )
            mock_ts.assert_called_once_with("story-1", "fail")

    @pytest.mark.asyncio
    async def test_admin_notified(self):
        """GIVE_UP should notify admins."""
        from src.consumers.deploy_failure_handler import _handle_give_up

        redis = MagicMock()
        redis.redis = AsyncMock()

        with (
            patch(f"{_PATCH}._transition_story_safe", new_callable=AsyncMock),
            patch(f"{_PATCH}.notify_admins", new_callable=AsyncMock) as mock_na,
            patch(
                f"{_PATCH}.get_story_worker",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await _handle_give_up(
                story_id="story-1",
                task_id="deploy-1",
                project_id="proj-1",
                error_details="port already allocated",
                redis=redis,
            )
            mock_na.assert_called_once()
            assert "port already allocated" in mock_na.call_args[0][0]

    @pytest.mark.asyncio
    async def test_worker_deleted_if_exists(self):
        """GIVE_UP should delete worker if one exists for the story."""
        from src.consumers.deploy_failure_handler import _handle_give_up

        redis = MagicMock()
        redis.redis = AsyncMock()

        with (
            patch(f"{_PATCH}._transition_story_safe", new_callable=AsyncMock),
            patch(f"{_PATCH}.notify_admins", new_callable=AsyncMock),
            patch(
                f"{_PATCH}.get_story_worker",
                new_callable=AsyncMock,
                return_value="worker-123",
            ),
            patch(f"{_PATCH}.delete_worker", new_callable=AsyncMock) as mock_dw,
            patch(f"{_PATCH}.clear_story_worker", new_callable=AsyncMock),
        ):
            await _handle_give_up(
                story_id="story-1",
                task_id="deploy-1",
                project_id="proj-1",
                error_details="port already allocated",
                redis=redis,
            )
            mock_dw.assert_called_once_with("worker-123", reason="failed")
