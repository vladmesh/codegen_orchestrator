"""Capability-backed temporary QA access is durable before dispatch."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from shared.contracts.dto.deploy_dispatch import DispatchWithdrawal
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.temporary_access import (
    TemporaryAccessGrantDTO,
    TemporaryAccessGrantUpdate,
    TemporaryAccessRevokeReason,
    TemporaryAccessStatus,
)
from shared.contracts.queues.deploy import DeployOutcome
from shared.contracts.queues.qa import QAMessage
from shared.queues import DEPLOY_QUEUE, QA_QUEUE

PROJECT_ID = "00000000-0000-0000-0000-000000000001"


def _message() -> QAMessage:
    return QAMessage(
        project_id=PROJECT_ID,
        initiating_run_id="live-1",
        telegram_chat_id="",
        deployed_url="https://exact.example.com",
        application_id=42,
        acceptance_criteria="the bot answers /start",
        run_id="qa-1",
    )


def _stored(request) -> TemporaryAccessGrantDTO:
    return TemporaryAccessGrantDTO(
        **request.model_dump(mode="json"),
        status=TemporaryAccessStatus.GRANTING,
        granted_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )


def _grant(**overrides) -> TemporaryAccessGrantDTO:
    now = datetime.now(UTC)
    values = {
        "id": "tempaccess-qa-1",
        "project_id": PROJECT_ID,
        "channel": "telegram",
        "external_id": "8202532144",
        "target_application_id": 42,
        "target_base_url": "https://exact.example.com",
        "head_sha": "a" * 40,
        "qa_run_id": "qa-1",
        "grant_run_id": "temporary-access-grant-old",
        "grant_attempts": 1,
        "qa_message": _message(),
        "status": TemporaryAccessStatus.GRANTING,
        "granted_at": now,
        "created_at": now,
    }
    values.update(overrides)
    return TemporaryAccessGrantDTO(**values)


def _operation_run(
    status: RunStatus,
    *,
    outcome: DeployOutcome | None = None,
    age_minutes: int = 0,
):
    return SimpleNamespace(
        status=status,
        result=SimpleNamespace(deploy_outcome=outcome) if outcome is not None else None,
        created_at=datetime.now(UTC) - timedelta(minutes=age_minutes),
    )


@pytest.mark.asyncio
async def test_grant_persists_the_verified_identity_and_exact_target_before_dispatch() -> None:
    from src.tasks.temporary_access import grant_temporary_access

    api = AsyncMock()
    # The target names the commit it is running, which is what a capability
    # redeploy has to ask for again.
    api.latest_deployed_commit_sha = AsyncMock(return_value="e" * 40)
    api.create_temporary_access_grant.side_effect = _stored
    api.get_run_if_missing_returns_none.return_value = None
    redis = AsyncMock()

    grant = await grant_temporary_access(
        api,
        redis,
        project_id=PROJECT_ID,
        target_application_id=42,
        target_base_url="https://exact.example.com",
        head_sha="a" * 40,
        qa_message=_message(),
    )

    request = api.create_temporary_access_grant.await_args.args[0]
    assert request.channel == "telegram"
    assert request.external_id == "8202532144"
    assert request.target_application_id == 42
    assert request.target_base_url == "https://exact.example.com"
    assert {"capability", "bot_token", "env_key"}.isdisjoint(request.model_dump())
    assert grant is not None
    published = redis.publish_message.await_args
    assert published.args[0] == DEPLOY_QUEUE
    assert published.args[1].env_overrides == {}
    assert api.create_temporary_access_grant.await_count == 1


@pytest.mark.asyncio
async def test_target_holder_conflict_defers_only_this_handoff() -> None:
    from src.tasks.temporary_access import grant_temporary_access

    api = AsyncMock()
    # The target names the commit it is running, which is what a capability
    # redeploy has to ask for again.
    api.latest_deployed_commit_sha = AsyncMock(return_value="e" * 40)
    api.create_temporary_access_grant.side_effect = httpx.HTTPStatusError(
        "conflict",
        request=httpx.Request("POST", "https://api/temporary-access-grants/"),
        response=httpx.Response(409),
    )
    redis = AsyncMock()

    grant = await grant_temporary_access(
        api,
        redis,
        project_id=PROJECT_ID,
        target_application_id=42,
        target_base_url="https://exact.example.com",
        head_sha="a" * 40,
        qa_message=_message(),
    )

    assert grant is None
    redis.publish_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_list_conflict_does_not_abort_the_access_sweep() -> None:
    from src.tasks.temporary_access import supervise_temporary_access

    api = AsyncMock()
    # The target names the commit it is running, which is what a capability
    # redeploy has to ask for again.
    api.latest_deployed_commit_sha = AsyncMock(return_value="e" * 40)
    api.list_temporary_access_grants_under_watch.side_effect = httpx.HTTPStatusError(
        "legacy",
        request=httpx.Request("GET", "https://api/temporary-access-grants/"),
        response=httpx.Response(409),
    )

    assert await supervise_temporary_access(api, AsyncMock()) == {
        "dispatched": 0,
        "released": 0,
        "revoked": 0,
        "expired": 0,
        "revoke_failed": 0,
        "escalated": 0,
    }


@pytest.mark.asyncio
async def test_stale_grant_retries_the_stored_target_with_a_recorded_bound() -> None:
    from src.tasks.temporary_access import supervise_temporary_access

    grant = _grant()
    api = AsyncMock()
    # The target names the commit it is running, which is what a capability
    # redeploy has to ask for again.
    api.latest_deployed_commit_sha = AsyncMock(return_value="e" * 40)
    api.list_temporary_access_grants_under_watch.return_value = [grant]
    api.get_run_if_missing_returns_none.return_value = _operation_run(
        RunStatus.RUNNING, age_minutes=16
    )
    redis = AsyncMock()

    await supervise_temporary_access(api, redis)

    update = api.update_temporary_access_grant.await_args.args[1]
    assert update.grant_attempts == 2
    assert update.grant_run_id is not None
    publish = redis.publish_message.await_args.args[1]
    assert publish.head_sha == grant.head_sha
    assert publish.project_id == grant.project_id
    assert publish.env_overrides == {}


@pytest.mark.asyncio
async def test_cancelled_grant_redispatches_the_exact_target_without_spending_budget() -> None:
    from src.tasks.temporary_access import supervise_temporary_access

    grant = _grant(grant_attempts=2)
    api = AsyncMock()
    # The target names the commit it is running, which is what a capability
    # redeploy has to ask for again.
    api.latest_deployed_commit_sha = AsyncMock(return_value="e" * 40)
    api.list_temporary_access_grants_under_watch.return_value = [grant]
    api.get_run_if_missing_returns_none.return_value = _operation_run(
        RunStatus.CANCELLED, outcome=DeployOutcome.CANCELLED
    )
    redis = AsyncMock()

    counts = await supervise_temporary_access(api, redis)

    update = api.update_temporary_access_grant.await_args.args[1]
    assert update.grant_attempts == grant.grant_attempts
    assert update.grant_run_id != grant.grant_run_id
    publish = redis.publish_message.await_args.args[1]
    assert publish.project_id == grant.project_id
    assert publish.head_sha == grant.head_sha
    assert counts["dispatched"] == 1


@pytest.mark.asyncio
async def test_grant_attempt_exhaustion_fails_handoff_then_starts_cleanup() -> None:
    from src.tasks.temporary_access import supervise_temporary_access

    grant = _grant(grant_attempts=3)
    api = AsyncMock()
    # The target names the commit it is running, which is what a capability
    # redeploy has to ask for again.
    api.latest_deployed_commit_sha = AsyncMock(return_value="e" * 40)
    api.list_temporary_access_grants_under_watch.return_value = [grant]
    api.get_run_if_missing_returns_none.return_value = _operation_run(RunStatus.FAILED)
    redis = AsyncMock()

    await supervise_temporary_access(api, redis)

    api.record_run_outcome_unless_settled.assert_awaited_once()
    updates = [call.args[1] for call in api.update_temporary_access_grant.await_args_list]
    assert any(
        update.status is TemporaryAccessStatus.REVOKING
        and update.revoke_reason is TemporaryAccessRevokeReason.GRANT_FAILED
        and update.revoke_attempts == 1
        for update in updates
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("qa_run", "grant_overrides", "reason"),
    [
        (_operation_run(RunStatus.COMPLETED), {}, TemporaryAccessRevokeReason.RUN_TERMINAL),
        (None, {}, TemporaryAccessRevokeReason.RUN_MISSING),
        (
            _operation_run(RunStatus.RUNNING),
            {"granted_at": datetime.now(UTC) - timedelta(minutes=61)},
            TemporaryAccessRevokeReason.EXPIRED,
        ),
    ],
)
async def test_granted_access_starts_cleanup_for_terminal_missing_or_expired_qa(
    qa_run, grant_overrides, reason
) -> None:
    from src.tasks.temporary_access import supervise_temporary_access

    grant = _grant(status=TemporaryAccessStatus.GRANTED, **grant_overrides)
    api = AsyncMock()
    # The target names the commit it is running, which is what a capability
    # redeploy has to ask for again.
    api.latest_deployed_commit_sha = AsyncMock(return_value="e" * 40)
    api.list_temporary_access_grants_under_watch.return_value = [grant]
    api.get_run_if_missing_returns_none.return_value = qa_run
    redis = AsyncMock()

    counts = await supervise_temporary_access(api, redis)

    update = api.update_temporary_access_grant.await_args.args[1]
    assert update.status is TemporaryAccessStatus.REVOKING
    assert update.revoke_reason is reason
    assert update.revoke_attempts == 1
    assert redis.publish_message.await_args.args[0] == DEPLOY_QUEUE
    assert counts["dispatched"] == 1


@pytest.mark.asyncio
async def test_cleanup_transitions_then_withdraws_and_fences_an_old_grant_dispatch() -> None:
    from src.tasks.temporary_access import supervise_temporary_access

    grant = _grant(status=TemporaryAccessStatus.GRANTED)
    api = AsyncMock()
    # The target names the commit it is running, which is what a capability
    # redeploy has to ask for again.
    api.latest_deployed_commit_sha = AsyncMock(return_value="e" * 40)
    api.list_temporary_access_grants_under_watch.return_value = [grant]
    api.get_run_if_missing_returns_none.return_value = _operation_run(RunStatus.COMPLETED)
    api.withdraw_deploy_dispatch.return_value = SimpleNamespace(
        outcome=DispatchWithdrawal.ALREADY_DISPATCHED
    )
    redis = AsyncMock()

    await supervise_temporary_access(api, redis)

    update_index = next(
        index
        for index, call in enumerate(api.method_calls)
        if call[0] == "update_temporary_access_grant"
        and call.args[1].status is TemporaryAccessStatus.REVOKING
    )
    withdrawal_index = next(
        index
        for index, call in enumerate(api.method_calls)
        if call[0] == "withdraw_deploy_dispatch"
    )
    assert update_index < withdrawal_index
    api.withdraw_deploy_dispatch.assert_awaited_once_with(
        grant.grant_run_id, "temporary access cleanup superseded grant operation"
    )
    assert redis.publish_message.await_args.args[1].fence_active_deploys is True


@pytest.mark.asyncio
async def test_qa_is_released_once_only_after_grant_proof() -> None:
    from src.tasks.temporary_access import supervise_temporary_access

    grant = _grant()
    proved_grant = _grant(status=TemporaryAccessStatus.GRANTED)
    api = AsyncMock()
    # The target names the commit it is running, which is what a capability
    # redeploy has to ask for again.
    api.latest_deployed_commit_sha = AsyncMock(return_value="e" * 40)
    api.list_temporary_access_grants_under_watch.return_value = [grant]
    api.get_run_if_missing_returns_none.side_effect = [
        _operation_run(RunStatus.COMPLETED, outcome=DeployOutcome.SUCCESS),
        _operation_run(RunStatus.RUNNING),
    ]
    api.update_temporary_access_grant.side_effect = [proved_grant, proved_grant]
    redis = AsyncMock()

    counts = await supervise_temporary_access(api, redis)

    assert redis.publish_message.await_args.args[0] == QA_QUEUE
    published = redis.publish_message.await_args.args[1]
    assert published.project_id == grant.qa_message.project_id
    assert published.run_id == grant.qa_message.run_id
    assert published.deployed_url == grant.qa_message.deployed_url
    assert published.application_id == grant.qa_message.application_id
    assert counts["released"] == 1


@pytest.mark.asyncio
async def test_stale_revoke_is_replaced_before_it_can_hold_access_forever() -> None:
    from src.tasks.temporary_access import supervise_temporary_access

    grant = _grant(
        status=TemporaryAccessStatus.REVOKING,
        revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
        revoke_run_id="temporary-access-revoke-old",
        revoke_attempts=1,
    )
    api = AsyncMock()
    # The target names the commit it is running, which is what a capability
    # redeploy has to ask for again.
    api.latest_deployed_commit_sha = AsyncMock(return_value="e" * 40)
    api.list_temporary_access_grants_under_watch.return_value = [grant]
    api.get_run_if_missing_returns_none.return_value = _operation_run(
        RunStatus.RUNNING, age_minutes=16
    )
    redis = AsyncMock()

    await supervise_temporary_access(api, redis)

    updates = [call.args[1] for call in api.update_temporary_access_grant.await_args_list]
    assert any(update.status is TemporaryAccessStatus.REVOKE_FAILED for update in updates)
    assert any(
        update.status is TemporaryAccessStatus.REVOKING
        and update.revoke_attempts == 2
        and update.revoke_reason is TemporaryAccessRevokeReason.RUN_TERMINAL
        for update in updates
    )
    assert redis.publish_message.await_args.args[1].head_sha == grant.head_sha


@pytest.mark.asyncio
async def test_cancelled_revoke_redispatches_the_exact_target_without_spending_budget() -> None:
    from src.tasks.temporary_access import supervise_temporary_access

    grant = _grant(
        status=TemporaryAccessStatus.REVOKING,
        revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
        revoke_run_id="temporary-access-revoke-cancelled",
        revoke_attempts=2,
    )
    api = AsyncMock()
    # The target names the commit it is running, which is what a capability
    # redeploy has to ask for again.
    api.latest_deployed_commit_sha = AsyncMock(return_value="e" * 40)
    api.list_temporary_access_grants_under_watch.return_value = [grant]
    api.get_run_if_missing_returns_none.return_value = _operation_run(
        RunStatus.CANCELLED, outcome=DeployOutcome.CANCELLED
    )
    redis = AsyncMock()

    counts = await supervise_temporary_access(api, redis)

    update = api.update_temporary_access_grant.await_args.args[1]
    assert update.status is TemporaryAccessStatus.REVOKING
    assert update.revoke_attempts == grant.revoke_attempts
    assert update.revoke_run_id != grant.revoke_run_id
    publish = redis.publish_message.await_args.args[1]
    assert publish.project_id == grant.project_id
    assert publish.head_sha == grant.head_sha
    assert counts["revoke_failed"] == 0
    assert counts["dispatched"] == 1


@pytest.mark.asyncio
async def test_cancelled_revoke_at_unrevoked_deadline_escalates_once_without_queue_churn() -> None:
    from src.tasks.temporary_access import _settle_revoke, _settle_revoke_failed

    expired = _grant(
        status=TemporaryAccessStatus.REVOKING,
        revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
        revoke_run_id="temporary-access-revoke-cancelled",
        revoke_attempts=1,
        granted_at=datetime.now(UTC) - timedelta(minutes=120),
    )
    settled = _grant(
        status=TemporaryAccessStatus.REVOKE_FAILED,
        revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
        revoke_attempts=1,
        escalated_at=datetime.now(UTC),
        granted_at=expired.granted_at,
    )
    api = AsyncMock()
    # The target names the commit it is running, which is what a capability
    # redeploy has to ask for again.
    api.latest_deployed_commit_sha = AsyncMock(return_value="e" * 40)
    api.get_run_if_missing_returns_none.return_value = _operation_run(
        RunStatus.CANCELLED, outcome=DeployOutcome.CANCELLED
    )
    redis = AsyncMock()
    counts = {
        "dispatched": 0,
        "released": 0,
        "revoked": 0,
        "expired": 0,
        "revoke_failed": 0,
        "escalated": 0,
    }

    with patch("src.tasks.temporary_access.notify_admins_best_effort", new=AsyncMock()) as notify:
        await _settle_revoke(api, redis, expired, counts, AsyncMock())
        await _settle_revoke_failed(api, redis, settled, counts, AsyncMock())

    api.escalate_temporary_access_grant.assert_awaited_once()
    notify.assert_awaited_once()
    redis.publish_message.assert_not_awaited()
    assert counts["dispatched"] == 0
    assert counts["escalated"] == 1


@pytest.mark.asyncio
async def test_revoke_marks_the_record_closed_only_after_a_proved_operation() -> None:
    from src.tasks.temporary_access import supervise_temporary_access

    grant = _grant(
        status=TemporaryAccessStatus.REVOKING,
        revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
        revoke_run_id="temporary-access-revoke-proof",
        revoke_attempts=1,
    )
    api = AsyncMock()
    # The target names the commit it is running, which is what a capability
    # redeploy has to ask for again.
    api.latest_deployed_commit_sha = AsyncMock(return_value="e" * 40)
    api.list_temporary_access_grants_under_watch.return_value = [grant]
    api.get_run_if_missing_returns_none.return_value = _operation_run(
        RunStatus.COMPLETED, outcome=DeployOutcome.SUCCESS
    )

    counts = await supervise_temporary_access(api, AsyncMock())

    api.update_temporary_access_grant.assert_awaited_once_with(
        grant.id, TemporaryAccessGrantUpdate(status=TemporaryAccessStatus.REVOKED)
    )
    assert counts["revoked"] == 1


@pytest.mark.asyncio
async def test_exhausted_revoke_escalates_once_and_never_republishes_access() -> None:
    from src.tasks.temporary_access import _settle_revoke, _settle_revoke_failed

    grant = _grant(
        status=TemporaryAccessStatus.REVOKING,
        revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
        revoke_run_id="temporary-access-revoke-last",
        revoke_attempts=3,
    )
    api = AsyncMock()
    # The target names the commit it is running, which is what a capability
    # redeploy has to ask for again.
    api.latest_deployed_commit_sha = AsyncMock(return_value="e" * 40)
    api.get_run_if_missing_returns_none.return_value = _operation_run(RunStatus.FAILED)
    counts = {
        "dispatched": 0,
        "released": 0,
        "revoked": 0,
        "expired": 0,
        "revoke_failed": 0,
        "escalated": 0,
    }
    with patch("src.tasks.temporary_access.notify_admins_best_effort", new=AsyncMock()) as notify:
        await _settle_revoke(api, AsyncMock(), grant, counts, AsyncMock())
        assert api.escalate_temporary_access_grant.await_count == 1
        notify.assert_awaited_once()
        settled = _grant(
            status=TemporaryAccessStatus.REVOKE_FAILED,
            revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
            revoke_attempts=3,
            escalated_at=datetime.now(UTC),
        )
        await _settle_revoke_failed(api, AsyncMock(), settled, counts, AsyncMock())

    assert api.escalate_temporary_access_grant.await_count == 1
    assert counts["dispatched"] == 0
