"""Unit tests for deploy success/smoke-failure result handlers.

Verifies that handlers store correct deploy_outcome in run.result
and do NOT perform story transitions (dispatcher's job).
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.dto.temporary_access import TemporaryAccessGrantDTO, TemporaryAccessStatus
from shared.contracts.queues.deploy import DeployMessage, DeployOutcome, DeployTrigger

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


def _temporary_grant(**overrides) -> TemporaryAccessGrantDTO:
    now = datetime.now(UTC)
    values = {
        "id": "tempaccess-qa-1",
        "project_id": "proj-1",
        "channel": "telegram",
        "external_id": "8202532144",
        "target_application_id": 42,
        "target_base_url": "https://exact.example.com",
        "head_sha": "a" * 40,
        "qa_run_id": "qa-1",
        "grant_run_id": "temporary-access-grant-1",
        "qa_message": {
            "project_id": "proj-1",
            "initiating_run_id": "deploy-1",
            "telegram_chat_id": "",
            "deployed_url": "https://exact.example.com",
            "application_id": 42,
            "acceptance_criteria": "bot admission",
            "run_id": "qa-1",
        },
        "status": TemporaryAccessStatus.GRANTING,
        "granted_at": now,
        "created_at": now,
    }
    values.update(overrides)
    return TemporaryAccessGrantDTO(**values)


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
                msg=_make_deploy_msg(),
            )

            for call in mock_api.method_calls:
                assert "transition_story" not in str(call)

            # No QA message published
            mock_redis.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_temporary_revoke_requires_inactive_proof_without_disclosing_capability(self):
        from src.consumers.deploy_result_handler import _handle_deploy_success

        mock_redis = AsyncMock()
        grant = _temporary_grant(
            status=TemporaryAccessStatus.REVOKING,
            revoke_run_id="temporary-access-revoke-1",
        )
        with (
            patch(f"{_HANDLER_PATCH}.api_client") as mock_api,
            patch(f"{_HANDLER_PATCH}.GeneratedServiceGrantClient") as grant_client,
        ):
            mock_api.patch = AsyncMock()
            # This story has no Product Brief, so nothing is seeded.
            mock_api.get_product_brief_by_story = AsyncMock(return_value=None)
            mock_api.get_temporary_access_grant = AsyncMock(return_value=grant)
            grant_client.return_value.revoke_and_resolve = AsyncMock(
                return_value=SimpleNamespace(active=False, failure=None)
            )
            result = await _handle_deploy_success(
                result={
                    "deployed_url": "https://other.example.com",
                    "secret_values": {"USERS_GRANT_CAPABILITY": "capability-value"},
                },
                smoke_result=None,
                task_id="temporary-access-revoke-1",
                project_id="proj-1",
                project=_project(),
                callback_stream="cb:1",
                telegram_chat_id="123",
                story_id="story-1",
                redis=mock_redis,
                msg=_make_deploy_msg(),
                application_id=42,
                temporary_access_grant=grant,
                temporary_access_operation="revoke",
            )

        assert result["status"] == "success"
        grant_client.assert_called_once_with("https://exact.example.com")
        grant_client.return_value.revoke_and_resolve.assert_awaited_once_with(
            channel="telegram", external_id="8202532144", capability="capability-value"
        )
        assert "capability-value" not in str(mock_api.patch.await_args)

    @pytest.mark.asyncio
    async def test_temporary_grant_failure_never_records_success_or_releases_handoff(self):
        from src.consumers.deploy_result_handler import _handle_deploy_success

        mock_redis = AsyncMock()
        with (
            patch(f"{_HANDLER_PATCH}.api_client") as mock_api,
            patch(f"{_HANDLER_PATCH}.GeneratedServiceGrantClient") as grant_client,
        ):
            mock_api.patch = AsyncMock()
            # This story has no Product Brief, so nothing is seeded.
            mock_api.get_product_brief_by_story = AsyncMock(return_value=None)
            mock_api.get_temporary_access_grant = AsyncMock(return_value=_temporary_grant())
            grant_client.return_value.grant_and_resolve = AsyncMock(
                return_value=SimpleNamespace(
                    active=False, failure=SimpleNamespace(value="inactive")
                )
            )
            result = await _handle_deploy_success(
                result={
                    "deployed_url": "https://exact.example.com",
                    "secret_values": {"USERS_GRANT_CAPABILITY": "capability-value"},
                },
                smoke_result=None,
                task_id="deploy-1",
                project_id="proj-1",
                project=_project(),
                callback_stream="cb:1",
                telegram_chat_id="123",
                story_id="story-1",
                redis=mock_redis,
                msg=_make_deploy_msg(),
                application_id=42,
                temporary_access_grant=_temporary_grant(),
                temporary_access_operation="grant",
            )

        assert result["status"] == "failed"
        patched = mock_api.patch.await_args.kwargs["json"]
        assert patched["result"]["deploy_outcome"] == DeployOutcome.OWNER_ACCESS_PROOF_FAILED.value
        assert "capability-value" not in str(patched)
        mock_redis.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_temporary_grant_requires_exact_target_and_active_readback(self):
        from src.consumers.deploy_result_handler import _apply_temporary_access_operation

        with (
            patch(f"{_HANDLER_PATCH}.api_client") as mock_api,
            patch(f"{_HANDLER_PATCH}.GeneratedServiceGrantClient") as grant_client,
        ):
            mock_api.get_temporary_access_grant = AsyncMock(return_value=_temporary_grant())
            grant_client.return_value.grant_and_resolve = AsyncMock(
                return_value=SimpleNamespace(active=True, failure=None)
            )
            mismatch = await _apply_temporary_access_operation(
                task_id="temporary-access-grant-1",
                project_id="proj-1",
                application_id=41,
                secret_values={"USERS_GRANT_CAPABILITY": "capability-value"},
                grant=_temporary_grant(),
                operation="grant",
            )
            proof = await _apply_temporary_access_operation(
                task_id="temporary-access-grant-1",
                project_id="proj-1",
                application_id=42,
                secret_values={"USERS_GRANT_CAPABILITY": "capability-value"},
                grant=_temporary_grant(),
                operation="grant",
            )

        assert mismatch == "temporary_access_target_mismatch"
        grant_client.return_value.grant_and_resolve.assert_awaited_once_with(
            channel="telegram", external_id="8202532144", capability="capability-value"
        )
        assert proof is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("operation", "current_grant"),
        [
            (
                "grant",
                _temporary_grant(
                    status=TemporaryAccessStatus.REVOKED,
                    revoked_at=datetime.now(UTC),
                ),
            ),
            (
                "revoke",
                _temporary_grant(
                    status=TemporaryAccessStatus.REVOKED,
                    revoke_run_id="temporary-access-revoke-1",
                    revoked_at=datetime.now(UTC),
                ),
            ),
        ],
    )
    async def test_temporary_operation_refuses_terminal_or_superseded_record_before_effect(
        self, operation, current_grant
    ):
        from src.consumers.deploy_result_handler import _apply_temporary_access_operation

        attempted = _temporary_grant(
            revoke_run_id="temporary-access-revoke-1" if operation == "revoke" else None,
            status=TemporaryAccessStatus.REVOKING
            if operation == "revoke"
            else TemporaryAccessStatus.GRANTING,
        )
        task_id = attempted.revoke_run_id if operation == "revoke" else attempted.grant_run_id
        with (
            patch(f"{_HANDLER_PATCH}.api_client") as mock_api,
            patch(f"{_HANDLER_PATCH}.GeneratedServiceGrantClient") as grant_client,
        ):
            mock_api.get_temporary_access_grant = AsyncMock(return_value=current_grant)

            refusal = await _apply_temporary_access_operation(
                task_id=task_id,
                project_id="proj-1",
                application_id=42,
                secret_values={"USERS_GRANT_CAPABILITY": "capability-value"},
                grant=attempted,
                operation=operation,
            )

        assert refusal == "temporary_access_operation_superseded"
        grant_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_delayed_grant_cannot_reapply_after_revoke_proved_inactive(self):
        from src.consumers.deploy_result_handler import _apply_temporary_access_operation

        stale_grant = _temporary_grant()
        revoked_grant = _temporary_grant(
            status=TemporaryAccessStatus.REVOKED,
            revoke_run_id="temporary-access-revoke-1",
            revoked_at=datetime.now(UTC),
        )
        with (
            patch(f"{_HANDLER_PATCH}.api_client") as mock_api,
            patch(f"{_HANDLER_PATCH}.GeneratedServiceGrantClient") as grant_client,
        ):
            mock_api.get_temporary_access_grant = AsyncMock(return_value=revoked_grant)

            refusal = await _apply_temporary_access_operation(
                task_id=stale_grant.grant_run_id,
                project_id="proj-1",
                application_id=42,
                secret_values={"USERS_GRANT_CAPABILITY": "capability-value"},
                grant=stale_grant,
                operation="grant",
            )

        assert refusal == "temporary_access_operation_superseded"
        grant_client.assert_not_called()


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
                msg=msg,
            )

            patch_call = mock_api.patch.call_args
            run_result = patch_call[1]["json"]["result"]
            assert run_result["deploy_outcome"] == DeployOutcome.RETRY.value

        assert result["status"] == "failed"
