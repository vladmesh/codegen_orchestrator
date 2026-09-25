"""Capability-backed temporary QA access is durable before dispatch."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from structlog.testing import capture_logs

from shared.contracts.dto.deploy_dispatch import DispatchWithdrawal
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.run_result import DeployRunResult, DeploySkipReason, QABlockerCategory
from shared.contracts.dto.temporary_access import (
    TemporaryAccessGrantDTO,
    TemporaryAccessGrantUpdate,
    TemporaryAccessRevokeReason,
    TemporaryAccessStatus,
)
from shared.contracts.queues.deploy import DeployAction, DeployOutcome
from shared.contracts.queues.qa import QAMessage
from shared.queues import DEPLOY_QUEUE, QA_QUEUE

PROJECT_ID = "00000000-0000-0000-0000-000000000001"


def _message() -> QAMessage:
    return QAMessage(
        story_id="story-1",
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
    skipped_reason: DeploySkipReason | None = None,
):
    return SimpleNamespace(
        status=status,
        result=(
            SimpleNamespace(deploy_outcome=outcome, skipped_reason=skipped_reason)
            if outcome is not None
            else None
        ),
        created_at=datetime.now(UTC) - timedelta(minutes=age_minutes),
    )


@pytest.mark.asyncio
async def test_skipped_grant_run_is_not_proof_and_retries_without_releasing_qa() -> None:
    from src.tasks.temporary_access import supervise_temporary_access

    grant = _grant()
    api = AsyncMock()
    api.latest_deployed_commit_sha = AsyncMock(return_value="e" * 40)
    api.list_temporary_access_grants_under_watch.return_value = [grant]
    api.get_run_if_missing_returns_none.return_value = _operation_run(
        RunStatus.COMPLETED,
        outcome=DeployOutcome.SUCCESS,
        skipped_reason=DeploySkipReason.ALREADY_DEPLOYED_SAME_SHA,
    )
    redis = AsyncMock()

    counts = await supervise_temporary_access(api, redis)

    updates = [call.args[1] for call in api.update_temporary_access_grant.await_args_list]
    assert all(update.status is not TemporaryAccessStatus.GRANTED for update in updates)
    assert updates[0].grant_attempts == grant.grant_attempts + 1
    assert updates[0].last_error == "grant proof failed"
    assert [call.args[0] for call in redis.publish_message.await_args_list] == [DEPLOY_QUEUE]
    assert counts["released"] == 0


@pytest.mark.asyncio
async def test_skipped_grant_run_at_the_bound_fails_qa_with_the_access_blocker() -> None:
    from src.tasks.temporary_access import _max_grant_attempts, supervise_temporary_access

    grant = _grant(grant_attempts=_max_grant_attempts())
    api = AsyncMock()
    api.latest_deployed_commit_sha = AsyncMock(return_value="e" * 40)
    api.list_temporary_access_grants_under_watch.return_value = [grant]
    api.get_run_if_missing_returns_none.return_value = _operation_run(
        RunStatus.COMPLETED,
        outcome=DeployOutcome.SUCCESS,
        skipped_reason=DeploySkipReason.ALREADY_DEPLOYED_SAME_SHA,
    )
    redis = AsyncMock()

    await supervise_temporary_access(api, redis)

    run_id, outcome = api.record_run_outcome_unless_settled.await_args.args
    assert run_id == grant.qa_run_id
    assert outcome["status"] == RunStatus.FAILED.value
    assert outcome["result"]["blocker"]["category"] == QABlockerCategory.QA_ACCESS_GRANT_FAILED
    assert QA_QUEUE not in [call.args[0] for call in redis.publish_message.await_args_list]
    updates = [call.args[1] for call in api.update_temporary_access_grant.await_args_list]
    assert any(
        update.status is TemporaryAccessStatus.REVOKING
        and update.revoke_reason is TemporaryAccessRevokeReason.GRANT_FAILED
        for update in updates
    )


@pytest.mark.asyncio
async def test_skipped_revoke_run_is_not_proof_and_never_closes_the_record() -> None:
    from src.tasks.temporary_access import supervise_temporary_access

    grant = _grant(
        status=TemporaryAccessStatus.REVOKING,
        revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
        revoke_run_id="temporary-access-revoke-skipped",
        revoke_attempts=1,
    )
    api = AsyncMock()
    api.latest_deployed_commit_sha = AsyncMock(return_value="e" * 40)
    api.list_temporary_access_grants_under_watch.return_value = [grant]
    api.get_run_if_missing_returns_none.return_value = _operation_run(
        RunStatus.COMPLETED,
        outcome=DeployOutcome.SUCCESS,
        skipped_reason=DeploySkipReason.ALREADY_DEPLOYED_SAME_SHA,
    )

    counts = await supervise_temporary_access(api, AsyncMock())

    updates = [call.args[1] for call in api.update_temporary_access_grant.await_args_list]
    assert all(update.status is not TemporaryAccessStatus.REVOKED for update in updates)
    assert any(update.status is TemporaryAccessStatus.REVOKE_FAILED for update in updates)
    assert counts["revoked"] == 0
    assert counts["revoke_failed"] == 1


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


def _held_api(*, holder, qa_run_age_minutes: float) -> AsyncMock:
    """An API whose create is refused because `holder` still holds the target."""
    api = AsyncMock()
    # The target names the commit it is running, which is what a capability
    # redeploy has to ask for again.
    api.latest_deployed_commit_sha = AsyncMock(return_value="e" * 40)
    api.create_temporary_access_grant.side_effect = httpx.HTTPStatusError(
        "conflict",
        request=httpx.Request("POST", "https://api/temporary-access-grants/"),
        response=httpx.Response(409),
    )
    api.live_temporary_access_grant_holding_target = AsyncMock(return_value=holder)
    api.get_run_if_missing_returns_none = AsyncMock(
        return_value=SimpleNamespace(
            status=RunStatus.QUEUED,
            result=None,
            created_at=datetime.now(UTC) - timedelta(minutes=qa_run_age_minutes),
        )
    )
    return api


async def _deferred_handoff(api, redis) -> TemporaryAccessGrantDTO | None:
    from src.tasks.temporary_access import grant_temporary_access

    return await grant_temporary_access(
        api,
        redis,
        project_id=PROJECT_ID,
        target_application_id=42,
        target_base_url="https://exact.example.com",
        head_sha="a" * 40,
        qa_message=_message(),
    )


@pytest.mark.asyncio
async def test_target_holder_conflict_defers_only_this_handoff() -> None:
    holder = _grant(id="tempaccess-qa-0", qa_run_id="qa-0")
    api = _held_api(holder=holder, qa_run_age_minutes=0)
    redis = AsyncMock()

    with capture_logs() as logs:
        grant = await _deferred_handoff(api, redis)

    assert grant is None
    redis.publish_message.assert_not_awaited()
    # Nothing about the refused run is settled while the wait is still inside
    # its bound: only the holder is reported.
    api.record_run_outcome_unless_settled.assert_not_awaited()
    deferred = [entry for entry in logs if entry["event"] == "temporary_access_handoff_deferred"]
    assert len(deferred) == 1
    assert deferred[0]["held_by"] == "tempaccess-qa-0"
    assert deferred[0]["held_by_status"] == TemporaryAccessStatus.GRANTING.value
    assert deferred[0]["held_by_qa_run_id"] == "qa-0"


@pytest.mark.asyncio
async def test_a_deferral_past_its_bound_fails_qa_and_names_the_holder() -> None:
    """The story reaches a verdict instead of waiting for something outside."""
    from src.tasks.temporary_access import _target_held_max_minutes

    holder = _grant(
        id="tempaccess-qa-0",
        qa_run_id="qa-0",
        status=TemporaryAccessStatus.REVOKE_FAILED,
        revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
        revoke_attempts=3,
    )
    api = _held_api(holder=holder, qa_run_age_minutes=_target_held_max_minutes())
    redis = AsyncMock()

    with patch("src.tasks.temporary_access.notify_admins_best_effort", AsyncMock()) as notified:
        grant = await _deferred_handoff(api, redis)

    assert grant is None
    redis.publish_message.assert_not_awaited()
    settled = api.record_run_outcome_unless_settled.await_args
    assert settled.args[0] == "qa-1"
    outcome = settled.args[1]
    assert outcome["status"] == RunStatus.FAILED.value
    blocker = outcome["result"]["blocker"]
    assert blocker["category"] == QABlockerCategory.QA_ACCESS_GRANT_FAILED.value
    # The verdict says which grant held the target and in what state.
    assert "tempaccess-qa-0" in blocker["received"]
    assert TemporaryAccessStatus.REVOKE_FAILED.value in blocker["received"]
    assert "tempaccess-qa-0" in notified.await_args.args[0]


@pytest.mark.asyncio
async def test_a_deferral_past_its_bound_reports_even_an_unreadable_holder() -> None:
    api = _held_api(holder=None, qa_run_age_minutes=99)

    with patch("src.tasks.temporary_access.notify_admins_best_effort", AsyncMock()):
        assert await _deferred_handoff(api, AsyncMock()) is None

    outcome = api.record_run_outcome_unless_settled.await_args.args[1]
    assert outcome["status"] == RunStatus.FAILED.value
    assert (
        outcome["result"]["blocker"]["category"] == QABlockerCategory.QA_ACCESS_GRANT_FAILED.value
    )


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
    from src.tasks.temporary_access import (
        _max_revoke_attempts,
        _settle_revoke,
        _settle_revoke_failed,
    )

    configured_attempt_limit = _max_revoke_attempts()

    grant = _grant(
        status=TemporaryAccessStatus.REVOKING,
        revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
        revoke_run_id="temporary-access-revoke-last",
        revoke_attempts=configured_attempt_limit,
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
            revoke_attempts=configured_attempt_limit,
            escalated_at=datetime.now(UTC),
        )
        await _settle_revoke_failed(api, AsyncMock(), settled, counts, AsyncMock())

    assert api.escalate_temporary_access_grant.await_count == 1
    assert counts["dispatched"] == 0


@pytest.mark.asyncio
async def test_exhausted_revoke_awaiting_qa_routing_keeps_cleaning_up_without_an_incident() -> None:
    """The API refuses the incident while the QA verdict is unrouted; that is a wait.

    No administrator is told and no attempt is spent, but a revoke still goes
    out. Once routing is recorded the next failed revoke escalates exactly once.
    """
    from src.tasks.temporary_access import _max_revoke_attempts, _settle_revoke

    attempts = _max_revoke_attempts()
    grant = _grant(
        status=TemporaryAccessStatus.REVOKING,
        revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
        revoke_run_id="temporary-access-revoke-last",
        revoke_attempts=attempts,
    )
    api = AsyncMock()
    api.latest_deployed_commit_sha = AsyncMock(return_value="e" * 40)
    api.get_run_if_missing_returns_none.return_value = _operation_run(RunStatus.FAILED)
    api.escalate_temporary_access_grant.return_value = None
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
        await _settle_revoke(api, redis, grant, counts, AsyncMock())

        notify.assert_not_awaited()
        update = api.update_temporary_access_grant.await_args.args[1]
        assert update.status is TemporaryAccessStatus.REVOKING
        assert update.revoke_attempts == attempts
        assert update.revoke_run_id != grant.revoke_run_id
        assert update.revoke_reason is TemporaryAccessRevokeReason.RUN_TERMINAL
        assert [call.args[0] for call in redis.publish_message.await_args_list] == [DEPLOY_QUEUE]
        assert counts["escalated"] == 0
        assert counts["dispatched"] == 1

        # The story has routed the verdict: the redispatched revoke failing
        # again now produces the one incident.
        api.escalate_temporary_access_grant.return_value = _grant(
            status=TemporaryAccessStatus.REVOKE_FAILED, escalated_at=datetime.now(UTC)
        )
        retried = grant.model_copy(update={"revoke_run_id": update.revoke_run_id})
        await _settle_revoke(api, redis, retried, counts, AsyncMock())

    notify.assert_awaited_once()
    assert api.escalate_temporary_access_grant.await_count == 2
    assert redis.publish_message.await_count == 1
    assert counts["escalated"] == 1


@pytest.mark.asyncio
async def test_revoke_that_found_no_deployment_settles_revoked_without_retry() -> None:
    """A revoke landing after the target was undeployed is a proved revoke.

    The deploy consumer allocates nothing for it and records the result a proved
    revoke records, because access to a deployment that no longer exists went
    with it. The supervisor closes the grant on that result: no `REVOKE_FAILED`,
    no replacement revoke and no administrator.
    """
    from src.tasks.temporary_access import _counts, _settle_granted, _settle_revoke

    grant = _grant(status=TemporaryAccessStatus.GRANTED)
    api = AsyncMock()
    api.latest_deployed_commit_sha = AsyncMock(return_value="e" * 40)
    api.get_run_if_missing_returns_none.return_value = SimpleNamespace(
        status=RunStatus.COMPLETED, created_at=datetime.now(UTC)
    )
    api.withdraw_deploy_dispatch.return_value = SimpleNamespace(
        outcome=DispatchWithdrawal.ALREADY_DISPATCHED
    )
    redis = AsyncMock()
    counts = _counts()

    # The QA run is terminal, so the supervisor dispatches the revoke.
    await _settle_granted(api, redis, grant, counts, AsyncMock())
    dispatched = api.update_temporary_access_grant.await_args.args[1]
    assert dispatched.status is TemporaryAccessStatus.REVOKING
    revoking = grant.model_copy(
        update={
            "status": TemporaryAccessStatus.REVOKING,
            "revoke_reason": dispatched.revoke_reason,
            "revoke_run_id": dispatched.revoke_run_id,
            "revoke_attempts": dispatched.revoke_attempts,
        }
    )
    api.update_temporary_access_grant.reset_mock()
    redis.publish_message.reset_mock()

    # What the deploy consumer records for a revoke whose target has no allocations.
    api.get_run_if_missing_returns_none.return_value = SimpleNamespace(
        status=RunStatus.COMPLETED,
        result=DeployRunResult(deploy_outcome=DeployOutcome.SUCCESS, action=DeployAction.FEATURE),
        created_at=datetime.now(UTC),
    )
    with patch("src.tasks.temporary_access.notify_admins_best_effort", new=AsyncMock()) as notify:
        await _settle_revoke(api, redis, revoking, counts, AsyncMock())

    api.update_temporary_access_grant.assert_awaited_once_with(
        grant.id, TemporaryAccessGrantUpdate(status=TemporaryAccessStatus.REVOKED)
    )
    redis.publish_message.assert_not_awaited()
    api.escalate_temporary_access_grant.assert_not_awaited()
    notify.assert_not_awaited()
    assert counts["revoked"] == 1
    assert counts["revoke_failed"] == 0
    assert counts["escalated"] == 0
