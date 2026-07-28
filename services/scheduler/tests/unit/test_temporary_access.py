"""Temporary access is granted and revoked by state, not by the tail of a run.

Every test here starts from a stored grant and nothing else: no in-process
handle, no caller still running. That is the point: whatever produced the grant
may be dead, and the sweep still has to finish the lifecycle — hand the access
over, start the QA run, and take the access back.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from _run_routing_factories import _make_run
import pytest

from shared.contracts.dto.deploy_dispatch import DeployDispatchWithdrawal, DispatchWithdrawal
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.temporary_access import (
    TemporaryAccessGrantDTO,
    TemporaryAccessRevokeReason,
    TemporaryAccessStatus,
)
from shared.contracts.queues.deploy import DeployMessage, DeployOutcome
from shared.contracts.queues.qa import QAMessage, QAOutcome
from shared.queues import DEPLOY_QUEUE, QA_QUEUE

PROJECT_ID = "00000000-0000-0000-0000-000000000001"
HEAD_SHA = "a" * 40
ENV_KEY = "TG_BOT_TEST_TELEGRAM_ID"


def _qa_message(run_id: str = "qa-1") -> QAMessage:
    return QAMessage(
        story_id="story-1",
        project_id=PROJECT_ID,
        user_id="",
        deployed_url="https://example.com",
        application_id=42,
        acceptance_criteria="the bot answers /start",
        bot_username="palindrome_bot",
        run_id=run_id,
    )


def _make_grant(**overrides) -> TemporaryAccessGrantDTO:
    qa_run_id = overrides.pop("qa_run_id", "qa-1")
    defaults = {
        "id": "tempaccess-1",
        "project_id": PROJECT_ID,
        "env_key": ENV_KEY,
        "subject": "424242",
        "head_sha": HEAD_SHA,
        "qa_run_id": qa_run_id,
        "grant_run_id": "deploy-grant-1",
        "qa_message": _qa_message(qa_run_id),
        "status": TemporaryAccessStatus.GRANTED,
        "granted_at": datetime.now(UTC),
        "qa_dispatched_at": datetime.now(UTC),
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return TemporaryAccessGrantDTO(**defaults)


def _granting(**overrides) -> TemporaryAccessGrantDTO:
    """A grant whose deploy has not confirmed, so QA has not started."""
    return _make_grant(status=TemporaryAccessStatus.GRANTING, qa_dispatched_at=None, **overrides)


def _deploy_run(status: RunStatus, outcome: DeployOutcome | None = None, **overrides):
    return _make_run(
        id=overrides.pop("id", "deploy-revoke-1"),
        type=RunType.DEPLOY,
        status=status,
        story_id=None,
        result={"deploy_outcome": outcome.value} if outcome is not None else None,
        **overrides,
    )


def _withdrawal(
    outcome: DispatchWithdrawal = DispatchWithdrawal.WITHDRAWN,
    *,
    run_id: str = "deploy-grant-1",
    claimed_at: datetime | None = None,
) -> DeployDispatchWithdrawal:
    return DeployDispatchWithdrawal(
        run_id=run_id,
        outcome=outcome,
        run_status=RunStatus.CANCELLED,
        claimed_at=claimed_at,
    )


@pytest.fixture
def api_client():
    client = AsyncMock()
    client.update_temporary_access_grant = AsyncMock()
    client.update_run = AsyncMock()
    client.create_run = AsyncMock()
    # Default: the grant deploy never left the system, so a withdrawal settles it.
    client.withdraw_deploy_dispatch = AsyncMock(return_value=_withdrawal())
    return client


@pytest.fixture
def redis_client():
    client = AsyncMock()
    client.publish_message = AsyncMock()
    return client


def _published(redis_client, queue) -> list:
    return [c.args[1] for c in redis_client.publish_message.call_args_list if c.args[0] == queue]


def _published_deploy(redis_client) -> DeployMessage:
    """The single deploy message the sweep published."""
    messages = _published(redis_client, DEPLOY_QUEUE)
    assert len(messages) == 1
    assert isinstance(messages[0], DeployMessage)
    return messages[0]


def _grant_updates(api_client) -> list:
    return [call.args[1] for call in api_client.update_temporary_access_grant.call_args_list]


class TestGrantIssuance:
    """The record exists before the access does, and QA waits for it."""

    @pytest.mark.asyncio
    async def test_grant_is_recorded_before_the_deploy_that_applies_it(
        self, api_client, redis_client
    ):
        from src.tasks.temporary_access import grant_temporary_access

        order = []
        api_client.create_temporary_access_grant.side_effect = lambda payload: (
            order.append("record")
            or _granting(
                id=payload.id,
                subject=payload.subject,
                grant_run_id=payload.grant_run_id,
            )
        )
        redis_client.publish_message.side_effect = lambda *a, **k: order.append("deploy")

        grant = await grant_temporary_access(
            api_client,
            redis_client,
            project_id=PROJECT_ID,
            env_key=ENV_KEY,
            subject="424242",
            head_sha=HEAD_SHA,
            qa_message=_qa_message(),
        )

        assert order == ["record", "deploy"]
        assert grant.status is TemporaryAccessStatus.GRANTING
        message = _published_deploy(redis_client)
        assert message.env_overrides == {ENV_KEY: "424242"}
        assert message.head_sha == HEAD_SHA
        assert message.task_id == grant.grant_run_id

    @pytest.mark.asyncio
    async def test_qa_does_not_start_until_the_access_is_applied(self, api_client, redis_client):
        """The handoff is held on the record, not published next to the deploy."""
        from src.tasks.temporary_access import grant_temporary_access

        api_client.create_temporary_access_grant.side_effect = lambda payload: _granting(
            grant_run_id=payload.grant_run_id
        )

        await grant_temporary_access(
            api_client,
            redis_client,
            project_id=PROJECT_ID,
            env_key=ENV_KEY,
            subject="424242",
            head_sha=HEAD_SHA,
            qa_message=_qa_message(),
        )

        assert _published(redis_client, QA_QUEUE) == []


class TestGrantInFlight:
    """Nothing happens to the access until the deploy that applies it answers."""

    @pytest.mark.asyncio
    async def test_confirmed_grant_releases_the_qa_run(self, api_client, redis_client):
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [_granting()]

        async def _read_run(run_id):
            if run_id == "deploy-grant-1":
                return _deploy_run(RunStatus.COMPLETED, DeployOutcome.SUCCESS, id=run_id)
            return _make_run(id="qa-1", type=RunType.QA, status=RunStatus.QUEUED, result=None)

        api_client.get_run_if_missing_returns_none.side_effect = _read_run

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["released"] == 1
        qa_messages = _published(redis_client, QA_QUEUE)
        assert len(qa_messages) == 1
        assert qa_messages[0].run_id == "qa-1"
        updates = _grant_updates(api_client)
        assert updates[0].status is TemporaryAccessStatus.GRANTED
        assert updates[1].qa_dispatched is True

    @pytest.mark.asyncio
    async def test_a_terminal_qa_run_does_not_revoke_before_the_grant_lands(
        self, api_client, redis_client
    ):
        """The reviewer's ordering case: a lagging grant deploy must not be overtaken.

        Revoking while the grant deploy is still in flight would clear a value
        that deploy then writes back, and the record would read revoked while
        the identity still has access.
        """
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [_granting()]

        async def _read_run(run_id):
            if run_id == "deploy-grant-1":
                return _deploy_run(RunStatus.RUNNING, id=run_id)
            return _make_run(id="qa-1", type=RunType.QA, status=RunStatus.CANCELLED, result=None)

        api_client.get_run_if_missing_returns_none.side_effect = _read_run

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts == {
            "dispatched": 0,
            "released": 0,
            "revoked": 0,
            "expired": 0,
            "revoke_failed": 0,
        }
        redis_client.publish_message.assert_not_called()
        api_client.update_temporary_access_grant.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_grant_confirmed_after_its_run_died_revokes_without_starting_qa(
        self, api_client, redis_client
    ):
        """The access landed late; it is taken back, and QA is not started on it."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [_granting()]

        async def _read_run(run_id):
            if run_id == "deploy-grant-1":
                return _deploy_run(RunStatus.COMPLETED, DeployOutcome.SUCCESS, id=run_id)
            return _make_run(id="qa-1", type=RunType.QA, status=RunStatus.CANCELLED, result=None)

        api_client.get_run_if_missing_returns_none.side_effect = _read_run

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["dispatched"] == 1
        assert counts["released"] == 0
        assert _published(redis_client, QA_QUEUE) == []
        assert _published_deploy(redis_client).env_overrides == {ENV_KEY: ""}

    @pytest.mark.asyncio
    async def test_a_lost_grant_deploy_is_asked_for_again(self, api_client, redis_client):
        """A process that died before publishing leaves the intent on the record."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [_granting()]
        api_client.get_run_if_missing_returns_none.return_value = None

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["dispatched"] == 1
        message = _published_deploy(redis_client)
        assert message.env_overrides == {ENV_KEY: "424242"}
        update = _grant_updates(api_client)[0]
        assert update.grant_run_id == message.task_id
        assert update.grant_run_id != "deploy-grant-1"

    @pytest.mark.asyncio
    async def test_superseded_grant_deploy_is_dispatched_again(self, api_client, redis_client):
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [_granting()]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.CANCELLED, id="deploy-grant-1"
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["dispatched"] == 1
        assert _published_deploy(redis_client).env_overrides == {ENV_KEY: "424242"}

    @pytest.mark.asyncio
    async def test_failed_grant_deploy_fails_the_qa_run_and_clears_the_slot(
        self, api_client, redis_client
    ):
        """Whether the value landed is unknown, so it is cleared and QA fails."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [_granting()]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.FAILED, DeployOutcome.GIVE_UP, id="deploy-grant-1"
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["dispatched"] == 1
        assert _published(redis_client, QA_QUEUE) == []
        assert _published_deploy(redis_client).env_overrides == {ENV_KEY: ""}

        run_id, patch = api_client.update_run.call_args.args
        assert run_id == "qa-1"
        assert patch["status"] == RunStatus.FAILED.value
        assert patch["result"]["blocker"]["category"] == "qa_access_grant_failed"
        update = _grant_updates(api_client)[-1]
        assert update.status is TemporaryAccessStatus.REVOKING
        assert update.revoke_reason is TemporaryAccessRevokeReason.GRANT_FAILED

    @pytest.mark.asyncio
    async def test_a_grant_that_never_confirms_is_cleared_by_timeout(
        self, api_client, redis_client
    ):
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [
            _granting(granted_at=datetime.now(UTC) - timedelta(minutes=61))
        ]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.QUEUED, id="deploy-grant-1"
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["dispatched"] == 1
        assert _published_deploy(redis_client).env_overrides == {ENV_KEY: ""}

    @pytest.mark.asyncio
    async def test_an_abandoned_grant_deploy_cannot_still_be_picked_up(
        self, api_client, redis_client
    ):
        """The queued grant deploy is withdrawn before anything clears the value.

        A fence reaches a deploy that already runs on Actions. This one never
        started, so the only thing that stops it is its run: cancelled here, and
        refused by the deploy consumer if the message is picked up afterwards.
        """
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [
            _granting(granted_at=datetime.now(UTC) - timedelta(minutes=61))
        ]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.QUEUED, id="deploy-grant-1"
        )

        order = []
        api_client.withdraw_deploy_dispatch.side_effect = lambda run_id, reason: (
            order.append(("withdraw", run_id)) or _withdrawal()
        )
        redis_client.publish_message.side_effect = lambda queue, message: order.append(
            (queue, message.env_overrides[ENV_KEY])
        )

        await supervise_temporary_access(api_client, redis_client)

        # The grant deploy is withdrawn before the clear goes out, not after it landed.
        assert order[0] == ("withdraw", "deploy-grant-1")
        assert order[-1] == (DEPLOY_QUEUE, "")

    @pytest.mark.asyncio
    async def test_a_grant_deploy_that_already_ended_is_not_re_cancelled(
        self, api_client, redis_client
    ):
        """Nothing rewrites the outcome of a deploy that reported one."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [_granting()]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.FAILED, DeployOutcome.GIVE_UP, id="deploy-grant-1"
        )

        await supervise_temporary_access(api_client, redis_client)

        api_client.withdraw_deploy_dispatch.assert_not_awaited()
        assert [call.args[0] for call in api_client.update_run.call_args_list] == ["qa-1"]


class TestRevocationTriggers:
    """Which states of the QA run release the grant."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "run_status",
        [RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.COMPLETED],
    )
    async def test_terminal_qa_run_clears_the_value(self, api_client, redis_client, run_status):
        """A QA run killed mid-flight leaves the access revoked, not standing."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [_make_grant()]
        api_client.get_run_if_missing_returns_none.return_value = _make_run(
            id="qa-1",
            type=RunType.QA,
            status=run_status,
            result={"qa_outcome": QAOutcome.FAILED.value} if run_status != "cancelled" else None,
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["dispatched"] == 1
        message = _published_deploy(redis_client)
        assert message.env_overrides == {ENV_KEY: ""}
        assert message.head_sha == HEAD_SHA
        update = _grant_updates(api_client)[-1]
        assert update.status is TemporaryAccessStatus.REVOKING
        assert update.revoke_reason is TemporaryAccessRevokeReason.RUN_TERMINAL
        assert update.revoke_run_id == message.task_id

    @pytest.mark.asyncio
    async def test_vanished_qa_run_clears_the_value(self, api_client, redis_client):
        """A run that no longer exists will never release the grant itself."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [_make_grant()]
        api_client.get_run_if_missing_returns_none.return_value = None

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["dispatched"] == 1
        assert _published_deploy(redis_client).env_overrides == {ENV_KEY: ""}
        update = _grant_updates(api_client)[-1]
        assert update.revoke_reason is TemporaryAccessRevokeReason.RUN_MISSING

    @pytest.mark.asyncio
    async def test_live_qa_run_keeps_the_access(self, api_client, redis_client):
        """QA still running, so the identity is still using the access."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [_make_grant()]
        api_client.get_run_if_missing_returns_none.return_value = _make_run(
            id="qa-1", type=RunType.QA, status=RunStatus.RUNNING, result=None
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts == {
            "dispatched": 0,
            "released": 0,
            "revoked": 0,
            "expired": 0,
            "revoke_failed": 0,
        }
        redis_client.publish_message.assert_not_called()
        api_client.update_temporary_access_grant.assert_not_called()

    @pytest.mark.asyncio
    async def test_grant_without_a_finishing_run_expires(
        self, api_client, redis_client, monkeypatch
    ):
        """A run that never finishes must not hold the access forever."""
        from src.tasks import temporary_access as module

        notified = []
        monkeypatch.setattr(
            module,
            "notify_admins_best_effort",
            AsyncMock(side_effect=lambda *a, **k: notified.append((a, k))),
        )

        api_client.list_live_temporary_access_grants.return_value = [
            _make_grant(granted_at=datetime.now(UTC) - timedelta(minutes=61))
        ]
        api_client.get_run_if_missing_returns_none.return_value = _make_run(
            id="qa-1", type=RunType.QA, status=RunStatus.RUNNING, result=None
        )

        counts = await module.supervise_temporary_access(api_client, redis_client)

        assert counts["expired"] == 1
        assert counts["dispatched"] == 1
        assert _published_deploy(redis_client).env_overrides == {ENV_KEY: ""}
        update = _grant_updates(api_client)[-1]
        assert update.revoke_reason is TemporaryAccessRevokeReason.EXPIRED
        # The timeout is its own event, not a silent side effect of the revoke.
        assert notified, "expiry must be reported, not handled quietly"
        # The run that outlived its access ends too, instead of continuing
        # against a bot that now refuses it.
        _, patch = api_client.update_run.call_args.args
        assert patch["result"]["blocker"]["category"] == "qa_access_expired"


class TestRevokeInFlight:
    """A dispatched revoke is followed to a terminal answer."""

    @pytest.mark.asyncio
    async def test_successful_revoke_deploy_closes_the_grant(self, api_client, redis_client):
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [
            _make_grant(
                status=TemporaryAccessStatus.REVOKING,
                revoke_run_id="deploy-revoke-1",
                revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
                revoke_attempts=1,
            )
        ]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.COMPLETED, DeployOutcome.SUCCESS
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["revoked"] == 1
        update = _grant_updates(api_client)[-1]
        assert update.status is TemporaryAccessStatus.REVOKED
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_running_revoke_deploy_is_left_alone(self, api_client, redis_client):
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [
            _make_grant(
                status=TemporaryAccessStatus.REVOKING,
                revoke_run_id="deploy-revoke-1",
                revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
                revoke_attempts=1,
            )
        ]
        api_client.get_run_if_missing_returns_none.return_value = _make_run(
            id="deploy-revoke-1",
            type=RunType.DEPLOY,
            status=RunStatus.RUNNING,
            story_id=None,
            result=None,
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts == {
            "dispatched": 0,
            "released": 0,
            "revoked": 0,
            "expired": 0,
            "revoke_failed": 0,
        }
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_superseded_revoke_deploy_is_dispatched_again(self, api_client, redis_client):
        """Losing the project's deploy lock is contention, not a QA failure."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [
            _make_grant(
                status=TemporaryAccessStatus.REVOKING,
                revoke_run_id="deploy-revoke-1",
                revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
                revoke_attempts=1,
            )
        ]
        api_client.get_run_if_missing_returns_none.return_value = _make_run(
            id="deploy-revoke-1",
            type=RunType.DEPLOY,
            status=RunStatus.CANCELLED,
            story_id=None,
            result=None,
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["dispatched"] == 1
        assert counts["revoke_failed"] == 0
        api_client.update_run.assert_not_called()
        assert _published_deploy(redis_client).env_overrides == {ENV_KEY: ""}

    @pytest.mark.asyncio
    async def test_abandoned_revoke_deploy_is_dispatched_again(self, api_client, redis_client):
        """The process that published the revoke died; the access is still out."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [
            _make_grant(
                status=TemporaryAccessStatus.REVOKING,
                revoke_run_id="deploy-revoke-1",
                revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
                revoke_attempts=1,
            )
        ]
        api_client.get_run_if_missing_returns_none.return_value = _make_run(
            id="deploy-revoke-1",
            type=RunType.DEPLOY,
            status=RunStatus.QUEUED,
            story_id=None,
            result=None,
            created_at=datetime.now(UTC) - timedelta(minutes=16),
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["dispatched"] == 1
        assert _published_deploy(redis_client).env_overrides == {ENV_KEY: ""}
        update = _grant_updates(api_client)[-1]
        assert update.revoke_attempts == 2
        assert update.revoke_reason is TemporaryAccessRevokeReason.RUN_TERMINAL

    @pytest.mark.asyncio
    async def test_one_failed_revoke_is_retried_without_failing_the_run(
        self, api_client, redis_client
    ):
        """A single failed deploy is a retry, not yet the QA run's outcome."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [
            _make_grant(
                status=TemporaryAccessStatus.REVOKING,
                revoke_run_id="deploy-revoke-1",
                revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
                revoke_attempts=1,
            )
        ]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.FAILED, DeployOutcome.GIVE_UP
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["revoke_failed"] == 1
        update = _grant_updates(api_client)[-1]
        assert update.status is TemporaryAccessStatus.REVOKE_FAILED
        assert update.escalated is None
        api_client.update_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_exhausted_revokes_fail_the_qa_run_and_are_reported(
        self, api_client, redis_client, monkeypatch
    ):
        """Access that could not be taken back becomes that QA run's failure."""
        from src.tasks import temporary_access as module

        notified = []
        monkeypatch.setattr(
            module,
            "notify_admins_best_effort",
            AsyncMock(side_effect=lambda *a, **k: notified.append((a, k))),
        )

        api_client.list_live_temporary_access_grants.return_value = [
            _make_grant(
                status=TemporaryAccessStatus.REVOKING,
                revoke_run_id="deploy-revoke-1",
                revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
                revoke_attempts=3,
            )
        ]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.FAILED, DeployOutcome.GIVE_UP
        )

        counts = await module.supervise_temporary_access(api_client, redis_client)

        assert counts["revoke_failed"] == 1
        update = _grant_updates(api_client)[-1]
        assert update.status is TemporaryAccessStatus.REVOKE_FAILED
        assert update.escalated is True
        assert "deploy-revoke-1" in update.last_error
        assert notified, "unrevoked access must be reported"

        run_id, patch = api_client.update_run.call_args.args
        assert run_id == "qa-1"
        assert patch["status"] == RunStatus.FAILED.value
        assert patch["result"]["qa_outcome"] == QAOutcome.BLOCKED.value
        assert patch["result"]["blocker"]["category"] == "qa_cleanup_failed"

    @pytest.mark.asyncio
    async def test_failed_revoke_is_retried_on_the_next_sweep(self, api_client, redis_client):
        """A revoke that failed stays live and is attempted again, same reason."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [
            _make_grant(
                status=TemporaryAccessStatus.REVOKE_FAILED,
                revoke_run_id="deploy-revoke-1",
                revoke_reason=TemporaryAccessRevokeReason.GRANT_FAILED,
                revoke_attempts=1,
                last_error="revoke deploy deploy-revoke-1 ended failed (give_up)",
            )
        ]

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["dispatched"] == 1
        assert _published_deploy(redis_client).env_overrides == {ENV_KEY: ""}
        update = _grant_updates(api_client)[-1]
        assert update.revoke_attempts == 2
        # The reason a failed grant must be cleared does not become "the run
        # finished" just because the run has since been failed.
        assert update.revoke_reason is TemporaryAccessRevokeReason.GRANT_FAILED

    @pytest.mark.asyncio
    async def test_one_unsettleable_grant_does_not_stop_the_others(self, api_client, redis_client):
        """A broken grant fails alone; the scheduler keeps sweeping."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [
            _make_grant(id="tempaccess-broken", qa_run_id="qa-broken"),
            _make_grant(id="tempaccess-ok", qa_run_id="qa-ok"),
        ]

        async def _read_run(run_id):
            if run_id == "qa-broken":
                raise RuntimeError("API is having a bad day")

        api_client.get_run_if_missing_returns_none.side_effect = _read_run

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["revoke_failed"] == 1
        assert counts["dispatched"] == 1
        assert _published_deploy(redis_client).env_overrides == {ENV_KEY: ""}


class TestRevokedGrants:
    """Revocation is a state, so repeating it is not an error."""

    @pytest.mark.asyncio
    async def test_revoked_grants_are_not_swept_again(self, api_client, redis_client):
        """The sweep reads live grants only; a revoked one is done with."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = []

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts == {
            "dispatched": 0,
            "released": 0,
            "revoked": 0,
            "expired": 0,
            "revoke_failed": 0,
        }
        api_client.get_run_if_missing_returns_none.assert_not_called()
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_revoking_twice_reaches_the_same_state(self, api_client, redis_client):
        """Two sweeps over the same settled revoke both end revoked."""
        from src.tasks.temporary_access import supervise_temporary_access

        grant = _make_grant(
            status=TemporaryAccessStatus.REVOKING,
            revoke_run_id="deploy-revoke-1",
            revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
            revoke_attempts=1,
        )
        api_client.list_live_temporary_access_grants.return_value = [grant]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.COMPLETED, DeployOutcome.SUCCESS
        )

        first = await supervise_temporary_access(api_client, redis_client)
        second = await supervise_temporary_access(api_client, redis_client)

        assert first["revoked"] == 1
        assert second["revoked"] == 1
        for update in _grant_updates(api_client):
            assert update.status is TemporaryAccessStatus.REVOKED


class TestEscalationOrdering:
    """The story is only let past a live grant after the QA run says why."""

    @pytest.mark.asyncio
    async def test_the_qa_run_is_failed_before_the_grant_is_escalated(
        self, api_client, redis_client, monkeypatch
    ):
        """Order matters: the escalation stamp is what stops the story waiting."""
        from src.tasks import temporary_access as module

        monkeypatch.setattr(module, "notify_admins_best_effort", AsyncMock())
        writes: list[str] = []
        api_client.update_run.side_effect = lambda *a, **k: writes.append("qa_run")
        api_client.update_temporary_access_grant.side_effect = lambda *a, **k: writes.append(
            "grant"
        )

        api_client.list_live_temporary_access_grants.return_value = [
            _make_grant(
                status=TemporaryAccessStatus.REVOKING,
                revoke_run_id="deploy-revoke-1",
                revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
                revoke_attempts=3,
            )
        ]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.FAILED, DeployOutcome.GIVE_UP
        )

        await module.supervise_temporary_access(api_client, redis_client)

        assert writes == ["qa_run", "grant"]

    @pytest.mark.asyncio
    async def test_a_qa_run_that_cannot_be_failed_leaves_the_grant_unescalated(
        self, api_client, redis_client, monkeypatch
    ):
        """A process that dies between the two writes must not open the bypass.

        The grant keeps holding the story back, which is the safe side: the
        access is still out and the run has not been told. The next sweep
        repeats both writes.
        """
        from src.tasks import temporary_access as module

        monkeypatch.setattr(module, "notify_admins_best_effort", AsyncMock())
        api_client.update_run.side_effect = RuntimeError("API died mid-write")

        api_client.list_live_temporary_access_grants.return_value = [
            _make_grant(
                status=TemporaryAccessStatus.REVOKING,
                revoke_run_id="deploy-revoke-1",
                revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
                revoke_attempts=3,
            )
        ]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.FAILED, DeployOutcome.GIVE_UP
        )

        await module.supervise_temporary_access(api_client, redis_client)

        assert all(update.escalated is not True for update in _grant_updates(api_client)), (
            "the grant must not be escalated while the QA run still reads as it did"
        )

        api_client.update_run.side_effect = None
        await module.supervise_temporary_access(api_client, redis_client)

        assert api_client.update_run.call_args.args[0] == "qa-1"
        assert _grant_updates(api_client)[-1].escalated is True


class TestRevokeFencesTheGrantDeploy:
    """A revoke has to be the last writer, not merely the latest request."""

    @pytest.mark.asyncio
    async def test_the_revoke_deploy_fences_earlier_deploys(self, api_client, redis_client):
        """The grant deploy may still be live on Actions when this is dispatched."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [_make_grant()]
        api_client.get_run_if_missing_returns_none.return_value = _make_run(
            id="qa-1", type=RunType.QA, status=RunStatus.CANCELLED, story_id="story-1"
        )

        await supervise_temporary_access(api_client, redis_client)

        assert _published_deploy(redis_client).fence_active_deploys is True

    @pytest.mark.asyncio
    async def test_the_grant_deploy_does_not_fence(self, api_client, redis_client):
        """Handing access out has nothing to outlive; only taking it back does."""
        from src.tasks.temporary_access import grant_temporary_access

        api_client.create_temporary_access_grant.return_value = _granting()

        await grant_temporary_access(
            api_client,
            redis_client,
            project_id=PROJECT_ID,
            env_key=ENV_KEY,
            subject="424242",
            head_sha=HEAD_SHA,
            qa_message=_qa_message(),
        )

        assert _published_deploy(redis_client).fence_active_deploys is False

    @pytest.mark.asyncio
    async def test_an_abandoned_grant_deploy_is_fenced_by_the_revoke_that_replaces_it(
        self, api_client, redis_client
    ):
        """The grant workflow is still running — exactly the ordering to exclude.

        The sweep gives up on it and clears the slot; that clear must stop the
        run that can still write the identity back, so it carries the fence.
        """
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [
            _granting(granted_at=datetime.now(UTC) - timedelta(hours=6))
        ]

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["dispatched"] == 1
        message = _published_deploy(redis_client)
        assert message.env_overrides == {ENV_KEY: ""}
        assert message.fence_active_deploys is True


class TestRevokeWaitsForADeployThatAlreadyLeft:
    """A grant deploy past the dispatch boundary is stopped where it now lives.

    The revoke's fence reads GitHub Actions. A worker that has claimed the
    dispatch but not yet reached GitHub is invisible to it, so clearing the value
    on that tick would record the grant revoked while that deploy writes the
    identity back. The withdrawal reports the crossing, and the revoke waits for
    the worker's own account of what it did.
    """

    @pytest.mark.asyncio
    async def test_a_grant_deploy_that_just_crossed_holds_the_revoke_back(
        self, api_client, redis_client
    ):
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [
            _granting(granted_at=datetime.now(UTC) - timedelta(minutes=61))
        ]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.RUNNING, id="deploy-grant-1"
        )
        api_client.withdraw_deploy_dispatch.return_value = _withdrawal(
            DispatchWithdrawal.ALREADY_DISPATCHED, claimed_at=datetime.now(UTC)
        )

        await supervise_temporary_access(api_client, redis_client)

        # Nothing cleared: the tick ends without a revoke.
        assert _published(redis_client, DEPLOY_QUEUE) == []
        assert _grant_updates(api_client) == []
        # The QA run is not left guessing while that plays out.
        assert api_client.update_run.await_args.args[0] == "qa-1"
        assert api_client.update_run.await_args.args[1]["status"] == RunStatus.FAILED.value

    @pytest.mark.asyncio
    async def test_an_old_claim_alone_never_releases_the_revoke(self, api_client, redis_client):
        """Elapsed time is not proof that a claimed deploy reached GitHub.

        A worker paused past any wait — or one whose claim answer came back late
        — still calls workflow_dispatch afterwards. Revoking on a clock would
        clear the value, find nothing to fence, record the grant revoked, and let
        that deploy put the identity back. So the claim ageing changes nothing.
        """
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [
            _granting(granted_at=datetime.now(UTC) - timedelta(minutes=61))
        ]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.CANCELLED, id="deploy-grant-1"
        )
        api_client.withdraw_deploy_dispatch.return_value = _withdrawal(
            DispatchWithdrawal.ALREADY_DISPATCHED,
            claimed_at=datetime.now(UTC) - timedelta(hours=4),
        )

        await supervise_temporary_access(api_client, redis_client)

        assert _published(redis_client, DEPLOY_QUEUE) == []

    @pytest.mark.asyncio
    async def test_a_claim_that_stays_unanswered_is_reported_rather_than_waited_out(
        self, api_client, redis_client
    ):
        """A worker that never says what it did is a visible event, not a silent loop."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [
            _granting(granted_at=datetime.now(UTC) - timedelta(minutes=61))
        ]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.CANCELLED, id="deploy-grant-1"
        )
        api_client.withdraw_deploy_dispatch.return_value = _withdrawal(
            DispatchWithdrawal.ALREADY_DISPATCHED,
            claimed_at=datetime.now(UTC) - timedelta(minutes=30),
        )

        with patch("src.tasks.temporary_access.notify_admins_best_effort", AsyncMock()) as notify:
            await supervise_temporary_access(api_client, redis_client)

        notify.assert_awaited_once()
        assert "cannot be revoked" in notify.await_args.args[0]
        assert _grant_updates(api_client)[0].last_error.startswith(
            "grant deploy dispatch unsettled"
        )
        assert _published(redis_client, DEPLOY_QUEUE) == []

    @pytest.mark.asyncio
    async def test_the_revoke_goes_out_once_the_claimer_records_its_outcome(
        self, api_client, redis_client
    ):
        """The worker's own result is what proves the boundary settled.

        Once it is written, whatever the worker put on GitHub Actions exists to
        be listed, so the revoke's fence can reach it and the clear is safe.
        """
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [
            _granting(granted_at=datetime.now(UTC) - timedelta(minutes=61))
        ]
        # Live when the sweep looks, terminal with a result once it withdrew.
        api_client.get_run_if_missing_returns_none.side_effect = [
            _deploy_run(RunStatus.RUNNING, id="deploy-grant-1"),
            _deploy_run(RunStatus.CANCELLED, DeployOutcome.CANCELLED, id="deploy-grant-1"),
        ]
        api_client.withdraw_deploy_dispatch.return_value = _withdrawal(
            DispatchWithdrawal.ALREADY_DISPATCHED,
            claimed_at=datetime.now(UTC),
        )

        await supervise_temporary_access(api_client, redis_client)

        message = _published_deploy(redis_client)
        assert message.env_overrides == {ENV_KEY: ""}
        assert message.fence_active_deploys is True

    @pytest.mark.asyncio
    async def test_a_worker_that_recorded_its_own_outcome_is_not_waited_for(
        self, api_client, redis_client
    ):
        """A run carrying a result is a worker that finished; nothing is in flight."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [
            _granting(granted_at=datetime.now(UTC) - timedelta(minutes=61))
        ]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.CANCELLED, DeployOutcome.CANCELLED, id="deploy-grant-1"
        )

        await supervise_temporary_access(api_client, redis_client)

        api_client.withdraw_deploy_dispatch.assert_not_awaited()
        assert _published_deploy(redis_client).env_overrides == {ENV_KEY: ""}
