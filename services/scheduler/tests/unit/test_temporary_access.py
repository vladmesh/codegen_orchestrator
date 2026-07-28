"""Temporary access is revoked by state, not by the tail of a successful run.

Every test here starts from a stored grant and nothing else: no in-process
handle, no caller still running. That is the point: whatever produced the grant
may be dead, and the sweep still has to take the access back.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from _run_routing_factories import _make_run
import pytest

from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.temporary_access import (
    TemporaryAccessGrantDTO,
    TemporaryAccessRevokeReason,
    TemporaryAccessStatus,
)
from shared.contracts.queues.deploy import DeployMessage, DeployOutcome
from shared.contracts.queues.qa import QAOutcome
from shared.queues import DEPLOY_QUEUE

PROJECT_ID = "00000000-0000-0000-0000-000000000001"
HEAD_SHA = "a" * 40
ENV_KEY = "TG_BOT_TEST_TELEGRAM_ID"


def _make_grant(**overrides) -> TemporaryAccessGrantDTO:
    defaults = {
        "id": "tempaccess-1",
        "project_id": PROJECT_ID,
        "env_key": ENV_KEY,
        "subject": "424242",
        "head_sha": HEAD_SHA,
        "qa_run_id": "qa-1",
        "status": TemporaryAccessStatus.GRANTED,
        "granted_at": datetime.now(UTC),
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return TemporaryAccessGrantDTO(**defaults)


def _deploy_run(status: RunStatus, outcome: DeployOutcome, **overrides):
    return _make_run(
        id=overrides.pop("id", "deploy-revoke-1"),
        type=RunType.DEPLOY,
        status=status,
        story_id=None,
        result={"deploy_outcome": outcome.value},
        **overrides,
    )


@pytest.fixture
def api_client():
    client = AsyncMock()
    client.update_temporary_access_grant = AsyncMock()
    client.update_run = AsyncMock()
    client.create_run = AsyncMock()
    return client


@pytest.fixture
def redis_client():
    client = AsyncMock()
    client.publish_message = AsyncMock()
    return client


def _published_deploy(redis_client) -> DeployMessage:
    """The deploy message the sweep published, validated as a DeployMessage."""
    assert redis_client.publish_message.call_count == 1
    queue, message = redis_client.publish_message.call_args.args
    assert queue == DEPLOY_QUEUE
    assert isinstance(message, DeployMessage)
    return message


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
        update = api_client.update_temporary_access_grant.call_args.args[1]
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
        update = api_client.update_temporary_access_grant.call_args.args[1]
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

        assert counts == {"dispatched": 0, "revoked": 0, "expired": 0, "revoke_failed": 0}
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
        update = api_client.update_temporary_access_grant.call_args.args[1]
        assert update.revoke_reason is TemporaryAccessRevokeReason.EXPIRED
        # The timeout is its own event, not a silent side effect of the revoke.
        assert notified, "expiry must be reported, not handled quietly"


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
        update = api_client.update_temporary_access_grant.call_args.args[1]
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

        assert counts == {"dispatched": 0, "revoked": 0, "expired": 0, "revoke_failed": 0}
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
        update = api_client.update_temporary_access_grant.call_args.args[1]
        assert update.revoke_attempts == 2
        assert update.revoke_reason is TemporaryAccessRevokeReason.RUN_TERMINAL

    @pytest.mark.asyncio
    async def test_failed_revoke_deploy_fails_the_qa_run_and_keeps_the_grant(
        self, api_client, redis_client
    ):
        """Access that could not be taken back is that QA run's failure."""
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
        update = api_client.update_temporary_access_grant.call_args.args[1]
        assert update.status is TemporaryAccessStatus.REVOKE_FAILED
        assert "deploy-revoke-1" in update.last_error

        run_id, patch = api_client.update_run.call_args.args
        assert run_id == "qa-1"
        assert patch["status"] == RunStatus.FAILED.value
        assert patch["result"]["qa_outcome"] == QAOutcome.BLOCKED.value
        assert patch["result"]["blocker"]["category"] == "qa_cleanup_failed"

    @pytest.mark.asyncio
    async def test_failed_revoke_is_retried_on_the_next_sweep(self, api_client, redis_client):
        """A revoke that failed stays live and is attempted again."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_live_temporary_access_grants.return_value = [
            _make_grant(
                status=TemporaryAccessStatus.REVOKE_FAILED,
                revoke_run_id="deploy-revoke-1",
                revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
                revoke_attempts=1,
                last_error="revoke deploy deploy-revoke-1 ended failed (give_up)",
            )
        ]
        api_client.get_run_if_missing_returns_none.return_value = _make_run(
            id="qa-1",
            type=RunType.QA,
            status=RunStatus.FAILED,
            result={
                "qa_outcome": QAOutcome.BLOCKED.value,
                "blocker": {
                    "category": "qa_cleanup_failed",
                    "attempted": "revoke temporary access",
                    "sent": "cleared value",
                    "received": "deploy failed",
                },
            },
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["dispatched"] == 1
        assert _published_deploy(redis_client).env_overrides == {ENV_KEY: ""}
        update = api_client.update_temporary_access_grant.call_args.args[1]
        assert update.revoke_attempts == 2

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

        assert counts == {"dispatched": 0, "revoked": 0, "expired": 0, "revoke_failed": 0}
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
        for call in api_client.update_temporary_access_grant.call_args_list:
            assert call.args[1].status is TemporaryAccessStatus.REVOKED


class TestGrantIssuance:
    """The record exists before the access does."""

    @pytest.mark.asyncio
    async def test_grant_is_recorded_before_the_deploy_that_applies_it(
        self, api_client, redis_client
    ):
        from src.tasks.temporary_access import grant_temporary_access

        order = []
        api_client.create_temporary_access_grant.side_effect = lambda payload: (
            order.append("record") or _make_grant(id=payload.id, subject=payload.subject)
        )
        redis_client.publish_message.side_effect = lambda *a, **k: order.append("deploy")

        grant, deploy_run_id = await grant_temporary_access(
            api_client,
            redis_client,
            project_id=PROJECT_ID,
            env_key=ENV_KEY,
            subject="424242",
            head_sha=HEAD_SHA,
            qa_run_id="qa-1",
        )

        assert order == ["record", "deploy"]
        assert grant.subject == "424242"
        message = _published_deploy(redis_client)
        assert message.env_overrides == {ENV_KEY: "424242"}
        assert message.head_sha == HEAD_SHA
        assert message.task_id == deploy_run_id
