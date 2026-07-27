"""Unit tests for the temporary private-bot QA access deploys."""

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.run_result import QATestAccessLifecycle
from src.consumers._qa_test_access import _dispatch_deploy, revoke_temporary_qa_access


@pytest.mark.asyncio
async def test_grant_deploys_the_deterministic_identity_and_waits_for_completion() -> None:
    redis = AsyncMock()
    with (
        patch("src.consumers._qa_test_access.api_client") as api,
        patch("src.consumers._qa_test_access._wait_for_deploy", new_callable=AsyncMock) as wait,
    ):
        api.post = AsyncMock()
        wait.return_value = True, "completed"
        _, succeeded, _ = await _dispatch_deploy(
            parent_run_id="qa-1",
            project_id="00000000-0000-0000-0000-000000000001",
            application_id=7,
            head_sha="a" * 40,
            phase="grant",
            env_overrides={"TG_BOT_TEST_TELEGRAM_ID": "8202532144"},
            redis=redis,
        )

    assert succeeded is True
    message = redis.publish_message.call_args.args[1]
    assert message.env_overrides == {"TG_BOT_TEST_TELEGRAM_ID": "8202532144"}
    wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_revocation_deploy_removes_the_temporary_identity() -> None:
    redis = AsyncMock()
    with (
        patch("src.consumers._qa_test_access.api_client") as api,
        patch("src.consumers._qa_test_access._wait_for_deploy", new_callable=AsyncMock) as wait,
    ):
        api.post = AsyncMock()
        wait.return_value = True, "completed"
        await _dispatch_deploy(
            parent_run_id="qa-1",
            project_id="00000000-0000-0000-0000-000000000001",
            application_id=7,
            head_sha="a" * 40,
            phase="revoke",
            env_overrides={},
            redis=redis,
        )

    message = redis.publish_message.call_args.args[1]
    assert message.env_overrides == {}
    assert api.post.call_args.kwargs["json"]["run_metadata"]["qa_test_access_phase"] == "revoke"


@pytest.mark.asyncio
async def test_revocation_waits_for_a_timed_out_grant_before_dispatching() -> None:
    redis = AsyncMock()
    lifecycle = QATestAccessLifecycle(in_test_mode=False, grant_run_id="grant-1")
    redis.redis.exists = AsyncMock(return_value=False)
    with (
        patch("src.consumers._qa_test_access._wait_for_deploy", new_callable=AsyncMock) as wait,
        patch("src.consumers._qa_test_access._dispatch_deploy", new_callable=AsyncMock) as dispatch,
        patch("src.consumers._qa_test_access.api_client") as api,
    ):
        wait.return_value = True, "completed"
        dispatch.return_value = "revoke-1", True, "completed"
        api.patch = AsyncMock()
        result, blocker = await revoke_temporary_qa_access(
            lifecycle=lifecycle,
            parent_run_id="qa-1",
            project_id="00000000-0000-0000-0000-000000000001",
            application_id=7,
            head_sha="a" * 40,
            redis=redis,
        )

    assert blocker is None
    assert result.revoke_succeeded is True
    wait.assert_awaited_once_with("grant-1")
    redis.redis.exists.assert_awaited_once_with("deploy:00000000-0000-0000-0000-000000000001:lock")
    dispatch.assert_awaited_once()
