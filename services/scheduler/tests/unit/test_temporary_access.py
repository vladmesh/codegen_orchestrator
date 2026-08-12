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

from shared.contracts.dto.application import ApplicationDTO, ApplicationStatus
from shared.contracts.dto.deploy_dispatch import (
    DISPATCH_SUPERSEDED_AT_KEY,
    DeployDispatchSupersede,
    DeployDispatchWithdrawal,
    DispatchSupersede,
    DispatchWithdrawal,
)
from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import QABlockerCategory
from shared.contracts.dto.temporary_access import (
    REVOKE_CONFIRMATION_READINGS,
    TemporaryAccessGrantDTO,
    TemporaryAccessObservation,
    TemporaryAccessRevokeReason,
    TemporaryAccessStatus,
)
from shared.contracts.queues.deploy import DeployMessage, DeployOutcome
from shared.contracts.queues.env_observation import (
    EnvObservationOutcome,
    EnvObservationRequest,
    EnvObservationResult,
    env_observation_pending_key,
    env_observation_result_key,
)
from shared.contracts.queues.qa import QAMessage, QAOutcome
from shared.queues import DEPLOY_QUEUE, ENV_OBSERVATION_QUEUE, QA_QUEUE

PROJECT_ID = "00000000-0000-0000-0000-000000000001"
HEAD_SHA = "a" * 40
ENV_KEY = "TG_BOT_TEST_TELEGRAM_ID"


def _qa_message(run_id: str = "qa-1") -> QAMessage:
    return QAMessage(
        story_id="story-1",
        project_id=PROJECT_ID,
        telegram_chat_id="",
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


def _supersede(
    outcome: DispatchSupersede,
    *,
    run_id: str = "deploy-grant-1",
    claimed_at: datetime | None = None,
) -> DeployDispatchSupersede:
    return DeployDispatchSupersede(
        run_id=run_id,
        outcome=outcome,
        run_status=RunStatus.CANCELLED,
        claimed_at=claimed_at or datetime.now(UTC),
        lease_expires_at=datetime.now(UTC),
    )


@pytest.fixture
def api_client():
    client = AsyncMock()
    client.update_temporary_access_grant = AsyncMock()
    client.update_run = AsyncMock()
    client.create_run = AsyncMock()
    # Default: nothing has been deployed for any run id yet.
    client.get_run_if_missing_returns_none = AsyncMock(return_value=None)
    client.record_run_outcome_unless_settled = AsyncMock(return_value=True)
    # Default: the grant deploy never left the system, so a withdrawal settles it.
    client.withdraw_deploy_dispatch = AsyncMock(return_value=_withdrawal())
    # Default: a claimed deploy is still inside its lease, so it is waited for.
    client.supersede_deploy_dispatch = AsyncMock(
        return_value=_supersede(DispatchSupersede.LEASE_LIVE)
    )
    client.escalate_temporary_access_grant = AsyncMock()
    # Default: the application the QA run tested is running on a known server of
    # this project, so the sweep has somewhere to send its reading.
    client.get_application_if_missing_returns_none = AsyncMock(return_value=_application())
    client.get_repositories = AsyncMock(return_value=[_repository()])
    client.get_project = AsyncMock(return_value=_project())
    # Default: the record took the reading and is not done with the grant yet.
    client.record_temporary_access_observation = AsyncMock(return_value=_under_confirmation())
    return client


def _application(
    status: ApplicationStatus = ApplicationStatus.RUNNING,
    *,
    application_id: int = 42,
    server_handle: str = "vps-1",
    repo_id: str = "repo-1",
) -> ApplicationDTO:
    return ApplicationDTO(
        id=application_id,
        repo_id=repo_id,
        server_handle=server_handle,
        service_name="backend",
        status=status,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _repository(repo_id: str = "repo-1") -> RepositoryDTO:
    return RepositoryDTO(
        id=repo_id,
        project_id=PROJECT_ID,
        name="palindrome-bot",
        git_url="https://github.com/vladmesh/palindrome-bot",
        role="primary",
        visibility="private",
        is_managed=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _recorded(**overrides) -> TemporaryAccessGrantDTO:
    """The grant as the record leaves it after a reading has been posted to it."""
    fields = {
        "status": TemporaryAccessStatus.REVOKING,
        "revoke_run_id": "deploy-revoke-1",
        "revoke_reason": TemporaryAccessRevokeReason.RUN_TERMINAL,
        "revoke_attempts": 1,
        "observed_at": datetime.now(UTC),
        "observation_id": "envobs-deploy-revoke-1-0",
        "slot_clear_since": datetime.now(UTC),
        "slot_clear_readings": 1,
    }
    fields.update(overrides)
    return _make_grant(**fields)


def _under_confirmation(readings: int = 1) -> TemporaryAccessGrantDTO:
    """A reading found the slot empty, and that is not yet enough to close it.

    One empty reading is a moment. The record keeps the grant under
    reconciliation until several taken over the confirmation window agree.
    """
    return _recorded(slot_clear_readings=readings)


def _confirmed_revoked() -> TemporaryAccessGrantDTO:
    """The record's answer once the readings have agreed for long enough."""
    return _recorded(
        slot_clear_readings=REVOKE_CONFIRMATION_READINGS,
        status=TemporaryAccessStatus.REVOKED,
        revoked_at=datetime.now(UTC),
    )


def _slot_still_filled() -> TemporaryAccessGrantDTO:
    """The record's answer to a reading that found the value: the streak restarts."""
    return _recorded(slot_clear_readings=0, slot_clear_since=None)


def _project() -> ProjectDTO:
    return ProjectDTO(
        id=PROJECT_ID,
        title="Palindrome",
        slug="palindrome-bot",
        owner_id=1,
        status=ProjectStatus.ACTIVE,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


class _FakeRedis:
    """The little bit of Redis the sweep uses to carry a question and its answer."""

    def __init__(self):
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        return sum(self.values.pop(key, None) is not None for key in keys)

    def answer(self, result: EnvObservationResult) -> None:
        """Leave the answer the reader would have left for this question."""
        self.values[env_observation_result_key(result.request_id)] = result.model_dump_json()


def _question(revoke_run_id: str = "deploy-revoke-1", readings: int = 0) -> str:
    """The id of one reading: which revoke attempt asked, and which reading it is."""
    return f"envobs-{revoke_run_id}-{readings}"


def _observed(present: bool, *, request_id: str = "") -> EnvObservationResult:
    return EnvObservationResult(
        request_id=request_id or _question(),
        outcome=EnvObservationOutcome.OBSERVED,
        env_key=ENV_KEY,
        present=present,
        containers=2,
    )


def _unreachable(*, request_id: str = "") -> EnvObservationResult:
    return EnvObservationResult(
        request_id=request_id or _question(),
        outcome=EnvObservationOutcome.UNREACHABLE,
        env_key=ENV_KEY,
        detail="the observation playbook failed: ssh: connect to host timed out",
    )


@pytest.fixture
def redis_client():
    client = AsyncMock()
    client.publish_message = AsyncMock()
    client.redis = _FakeRedis()
    return client


def _published(redis_client, queue) -> list:
    return [c.args[1] for c in redis_client.publish_message.call_args_list if c.args[0] == queue]


def _published_deploy(redis_client) -> DeployMessage:
    """The single deploy message the sweep published."""
    messages = _published(redis_client, DEPLOY_QUEUE)
    assert len(messages) == 1
    assert isinstance(messages[0], DeployMessage)
    return messages[0]


def _no_action() -> dict[str, int]:
    """A sweep that decided nothing this tick."""
    return {
        "dispatched": 0,
        "released": 0,
        "revoked": 0,
        "expired": 0,
        "revoke_failed": 0,
        "escalated": 0,
    }


def _grant_updates(api_client) -> list:
    return [call.args[1] for call in api_client.update_temporary_access_grant.call_args_list]


def _escalation(api_client) -> tuple[str, dict]:
    """The single escalation the sweep asked for: (grant id, keyword payload)."""
    call = api_client.escalate_temporary_access_grant.call_args
    assert call is not None, "the sweep never escalated"
    return call.args[0], call.kwargs


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

    @pytest.mark.asyncio
    async def test_a_retry_deploys_under_the_recorded_run_not_the_one_it_proposed(
        self, api_client, redis_client
    ):
        """The interleaving a fresh id per call leaves open.

        The first attempt commits the record and its response is lost. The retry
        proposes a new run id, but the record already names one and comes back
        holding it. Deploying under the proposed id would put the identity on the
        application through a run nothing is watching: the sweep follows the
        recorded id, revokes once its QA run ends, and the untracked deploy then
        writes the value back onto a grant already marked revoked.
        """
        from src.tasks.temporary_access import grant_temporary_access

        proposed: list[str] = []

        def _return_the_stored_record(payload):
            proposed.append(payload.grant_run_id)
            return _granting(grant_run_id="deploy-grant-committed")

        api_client.create_temporary_access_grant.side_effect = _return_the_stored_record

        grant = await grant_temporary_access(
            api_client,
            redis_client,
            project_id=PROJECT_ID,
            env_key=ENV_KEY,
            subject="424242",
            head_sha=HEAD_SHA,
            qa_message=_qa_message(),
        )

        assert proposed and proposed[0] != "deploy-grant-committed"
        assert grant.grant_run_id == "deploy-grant-committed"
        assert _published_deploy(redis_client).task_id == "deploy-grant-committed"
        assert api_client.create_run.call_args.args[0]["id"] == "deploy-grant-committed"

    @pytest.mark.asyncio
    async def test_a_retry_does_not_deploy_again_when_the_first_one_already_did(
        self, api_client, redis_client
    ):
        """The recorded run exists, so the access is already on its way out."""
        from src.tasks.temporary_access import grant_temporary_access

        api_client.create_temporary_access_grant.return_value = _granting(
            grant_run_id="deploy-grant-committed"
        )
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.RUNNING, id="deploy-grant-committed"
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

        assert _published(redis_client, DEPLOY_QUEUE) == []
        api_client.create_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_retry_does_not_reapply_access_the_sweep_is_taking_back(
        self, api_client, redis_client
    ):
        """A handoff repeating late must not undo a revoke already in flight."""
        from src.tasks.temporary_access import grant_temporary_access

        api_client.create_temporary_access_grant.return_value = _make_grant(
            status=TemporaryAccessStatus.REVOKING,
            revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
            revoke_run_id="deploy-revoke-1",
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

        assert _published(redis_client, DEPLOY_QUEUE) == []
        assert _published(redis_client, QA_QUEUE) == []


class TestGrantInFlight:
    """Nothing happens to the access until the deploy that applies it answers."""

    @pytest.mark.asyncio
    async def test_confirmed_grant_releases_the_qa_run(self, api_client, redis_client):
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [_granting()]

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

        api_client.list_temporary_access_grants_under_watch.return_value = [_granting()]

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
            "escalated": 0,
        }
        redis_client.publish_message.assert_not_called()
        api_client.update_temporary_access_grant.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_grant_confirmed_after_its_run_died_revokes_without_starting_qa(
        self, api_client, redis_client
    ):
        """The access landed late; it is taken back, and QA is not started on it."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [_granting()]

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

        api_client.list_temporary_access_grants_under_watch.return_value = [_granting()]
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

        api_client.list_temporary_access_grants_under_watch.return_value = [_granting()]
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

        api_client.list_temporary_access_grants_under_watch.return_value = [_granting()]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.FAILED, DeployOutcome.GIVE_UP, id="deploy-grant-1"
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["dispatched"] == 1
        assert _published(redis_client, QA_QUEUE) == []
        assert _published_deploy(redis_client).env_overrides == {ENV_KEY: ""}

        run_id, patch = api_client.record_run_outcome_unless_settled.call_args.args
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

        api_client.list_temporary_access_grants_under_watch.return_value = [
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

        api_client.list_temporary_access_grants_under_watch.return_value = [
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

        api_client.list_temporary_access_grants_under_watch.return_value = [_granting()]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.FAILED, DeployOutcome.GIVE_UP, id="deploy-grant-1"
        )

        await supervise_temporary_access(api_client, redis_client)

        api_client.withdraw_deploy_dispatch.assert_not_awaited()
        assert [
            call.args[0] for call in api_client.record_run_outcome_unless_settled.call_args_list
        ] == ["qa-1"]


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

        api_client.list_temporary_access_grants_under_watch.return_value = [_make_grant()]
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

        api_client.list_temporary_access_grants_under_watch.return_value = [_make_grant()]
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

        api_client.list_temporary_access_grants_under_watch.return_value = [_make_grant()]
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
            "escalated": 0,
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

        api_client.list_temporary_access_grants_under_watch.return_value = [
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
        _, patch = api_client.record_run_outcome_unless_settled.call_args.args
        assert patch["result"]["blocker"]["category"] == "qa_access_expired"

    @pytest.mark.asyncio
    async def test_a_run_that_finished_first_keeps_its_own_outcome_and_still_revokes(
        self, api_client, redis_client, monkeypatch
    ):
        """The other side of the race the API's outcome rule decides.

        The sweep reads a live QA run, declares the access expired, and by the
        time it writes, the run has recorded its own answer. The run keeps it —
        the sweep is not the one who got there first — and the access is taken
        back regardless, because it is out either way.
        """
        from src.tasks import temporary_access as module

        monkeypatch.setattr(module, "notify_admins_best_effort", AsyncMock())
        api_client.list_temporary_access_grants_under_watch.return_value = [
            _make_grant(granted_at=datetime.now(UTC) - timedelta(minutes=61))
        ]
        api_client.get_run_if_missing_returns_none.return_value = _make_run(
            id="qa-1", type=RunType.QA, status=RunStatus.RUNNING, result=None
        )
        api_client.record_run_outcome_unless_settled.return_value = False

        counts = await module.supervise_temporary_access(api_client, redis_client)

        assert counts["expired"] == 1
        assert counts["dispatched"] == 1
        assert counts["revoke_failed"] == 0
        assert _published_deploy(redis_client).env_overrides == {ENV_KEY: ""}
        assert _grant_updates(api_client)[-1].revoke_reason is TemporaryAccessRevokeReason.EXPIRED


class TestRevokeInFlight:
    """A dispatched revoke is followed to a terminal answer."""

    @pytest.mark.asyncio
    async def test_a_successful_revoke_deploy_confirmed_by_the_server_closes_the_grant(
        self, api_client, redis_client
    ):
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [
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
        redis_client.redis.answer(_observed(present=False))
        api_client.record_temporary_access_observation.return_value = _confirmed_revoked()

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["revoked"] == 1
        assert _published(redis_client, DEPLOY_QUEUE) == []

    @pytest.mark.asyncio
    async def test_running_revoke_deploy_is_left_alone(self, api_client, redis_client):
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [
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
            "escalated": 0,
        }
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_superseded_revoke_deploy_is_dispatched_again(self, api_client, redis_client):
        """Losing the project's deploy lock is contention, not a QA failure."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [
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
        api_client.record_run_outcome_unless_settled.assert_not_called()
        assert _published_deploy(redis_client).env_overrides == {ENV_KEY: ""}

    @pytest.mark.asyncio
    async def test_abandoned_revoke_deploy_is_dispatched_again(self, api_client, redis_client):
        """The process that published the revoke died; the access is still out."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [
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

        api_client.list_temporary_access_grants_under_watch.return_value = [
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
        api_client.record_run_outcome_unless_settled.assert_not_called()

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

        api_client.list_temporary_access_grants_under_watch.return_value = [
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
        assert notified, "unrevoked access must be reported"

        grant_id, escalation = _escalation(api_client)
        assert grant_id == "tempaccess-1"
        assert "deploy-revoke-1" in escalation["error"]
        assert escalation["run_result"].qa_outcome is QAOutcome.BLOCKED
        assert escalation["run_result"].blocker.category is QABlockerCategory.QA_CLEANUP_FAILED

    @pytest.mark.asyncio
    async def test_failed_revoke_is_retried_on_the_next_sweep(self, api_client, redis_client):
        """A revoke that failed stays live and is attempted again, same reason."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [
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

        api_client.list_temporary_access_grants_under_watch.return_value = [
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

        api_client.list_temporary_access_grants_under_watch.return_value = []

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts == {
            "dispatched": 0,
            "released": 0,
            "revoked": 0,
            "expired": 0,
            "revoke_failed": 0,
            "escalated": 0,
        }
        api_client.get_run_if_missing_returns_none.assert_not_called()
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_reporting_the_same_reading_twice_reaches_the_same_state(
        self, api_client, redis_client
    ):
        """A sweep repeating what it could not confirm gets the same answer back.

        The reading names itself, so the record counts it once however often it
        is delivered, and both sweeps read the grant back closed.
        """
        from src.tasks.temporary_access import supervise_temporary_access

        grant = _make_grant(
            status=TemporaryAccessStatus.REVOKING,
            revoke_run_id="deploy-revoke-1",
            revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
            revoke_attempts=1,
        )
        api_client.list_temporary_access_grants_under_watch.return_value = [grant]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.COMPLETED, DeployOutcome.SUCCESS
        )
        api_client.record_temporary_access_observation.return_value = _confirmed_revoked()

        redis_client.redis.answer(_observed(present=False))
        first = await supervise_temporary_access(api_client, redis_client)
        redis_client.redis.answer(_observed(present=False))
        second = await supervise_temporary_access(api_client, redis_client)

        assert first["revoked"] == 1
        assert second["revoked"] == 1
        assert {reading.observation_id for reading in _posted(api_client)} == {_question()}
        assert _grant_updates(api_client) == []


class TestEscalationIsOneDecision:
    """Giving up on a revoke is one write: the run's failure and the stamp together."""

    @pytest.mark.asyncio
    async def test_a_qa_run_that_already_passed_still_gets_the_cleanup_failure(
        self, api_client, redis_client, monkeypatch
    ):
        """The reviewed hole: a passed run that can never be told the access is stuck.

        The worker inside the run finished and recorded `passed` long before the
        revokes ran out. A write that steps aside for the first recorded outcome
        would leave the run reading `passed` with the identity still admitted,
        and the story waiting on a grant that has already given up. Cleanup is
        part of the run, so its failure is the run's, whatever the worker said.
        """
        from src.tasks import temporary_access as module

        monkeypatch.setattr(module, "notify_admins_best_effort", AsyncMock())
        api_client.list_temporary_access_grants_under_watch.return_value = [
            _make_grant(
                status=TemporaryAccessStatus.REVOKING,
                revoke_run_id="deploy-revoke-1",
                revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
                revoke_attempts=3,
            )
        ]

        async def _read_run(run_id):
            if run_id == "qa-1":
                # The QA worker's own verdict, already terminal and frozen.
                return _make_run(
                    id="qa-1",
                    type=RunType.QA,
                    status=RunStatus.COMPLETED,
                    story_id="story-1",
                    result={"qa_outcome": QAOutcome.PASSED.value, "summary": "the bot answered"},
                )
            return _deploy_run(RunStatus.FAILED, DeployOutcome.GIVE_UP)

        api_client.get_run_if_missing_returns_none.side_effect = _read_run

        await module.supervise_temporary_access(api_client, redis_client)

        _, escalation = _escalation(api_client)
        assert escalation["run_result"].qa_outcome is QAOutcome.BLOCKED
        assert escalation["run_result"].blocker.category is QABlockerCategory.QA_CLEANUP_FAILED
        assert ENV_KEY in escalation["run_error_message"]
        # Nothing tried the ordinary run patch, which would have been refused and
        # left the run reading `passed`.
        api_client.record_run_outcome_unless_settled.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_escalation_that_does_not_land_leaves_the_grant_holding_the_story(
        self, api_client, redis_client, monkeypatch
    ):
        """A dead process must not open the story's gate on its own.

        The stamp and the run's failure are one API call precisely so there is no
        state where one landed and the other did not. If the call never lands,
        the grant keeps holding the story back — the safe side — and the next
        sweep repeats it.
        """
        from src.tasks import temporary_access as module

        monkeypatch.setattr(module, "notify_admins_best_effort", AsyncMock())
        api_client.escalate_temporary_access_grant.side_effect = RuntimeError("API died mid-write")
        api_client.list_temporary_access_grants_under_watch.return_value = [
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
            "nothing may stamp the grant outside the call that fails the run"
        )

        api_client.escalate_temporary_access_grant.side_effect = None
        await module.supervise_temporary_access(api_client, redis_client)

        assert _escalation(api_client)[0] == "tempaccess-1"


class TestRevokeFencesTheGrantDeploy:
    """A revoke has to be the last writer, not merely the latest request."""

    @pytest.mark.asyncio
    async def test_the_revoke_deploy_fences_earlier_deploys(self, api_client, redis_client):
        """The grant deploy may still be live on Actions when this is dispatched."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [_make_grant()]
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

        api_client.list_temporary_access_grants_under_watch.return_value = [
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

        api_client.list_temporary_access_grants_under_watch.return_value = [
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
        assert api_client.record_run_outcome_unless_settled.await_args.args[0] == "qa-1"
        assert (
            api_client.record_run_outcome_unless_settled.await_args.args[1]["status"]
            == RunStatus.FAILED.value
        )

    @pytest.mark.asyncio
    async def test_a_worker_that_never_returns_loses_the_claim_and_the_revoke_goes_out(
        self, api_client, redis_client
    ):
        """The process-death case: a claimed grant deploy whose worker is gone.

        Waiting for its own account of what it did would be waiting forever, and
        the identity would stay admitted with an alert as the only trace. The
        claim it holds is a lease, and once that has run out the boundary is
        closed against it: it can neither dispatch nor re-claim, so anything it
        did put on Actions is there to be fenced. The revoke goes out on the same
        tick, carrying that fence.
        """
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [
            _granting(granted_at=datetime.now(UTC) - timedelta(minutes=61))
        ]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.RUNNING, id="deploy-grant-1"
        )
        api_client.withdraw_deploy_dispatch.return_value = _withdrawal(
            DispatchWithdrawal.ALREADY_DISPATCHED,
            claimed_at=datetime.now(UTC) - timedelta(hours=4),
        )
        api_client.supersede_deploy_dispatch.return_value = _supersede(DispatchSupersede.SUPERSEDED)

        await supervise_temporary_access(api_client, redis_client)

        message = _published_deploy(redis_client)
        assert message.env_overrides == {ENV_KEY: ""}
        assert message.fence_active_deploys is True

    @pytest.mark.asyncio
    async def test_a_claim_taken_back_before_the_restart_needs_no_second_supersede(
        self, api_client, redis_client
    ):
        """The stamp survives the sweep that wrote it, so a restart reads it and goes on.

        This is the same grant one process later. Nothing is in memory; the run
        carries the record that its claim was taken back, and that is enough to
        revoke against.
        """
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [
            _granting(granted_at=datetime.now(UTC) - timedelta(minutes=61))
        ]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.CANCELLED,
            id="deploy-grant-1",
            run_metadata={DISPATCH_SUPERSEDED_AT_KEY: datetime.now(UTC).isoformat()},
        )

        await supervise_temporary_access(api_client, redis_client)

        api_client.withdraw_deploy_dispatch.assert_not_awaited()
        api_client.supersede_deploy_dispatch.assert_not_awaited()
        assert _published_deploy(redis_client).env_overrides == {ENV_KEY: ""}

    @pytest.mark.asyncio
    async def test_a_claim_that_stays_unanswered_is_reported_rather_than_waited_out(
        self, api_client, redis_client
    ):
        """A worker that never says what it did is a visible event, not a silent loop."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [
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

        api_client.list_temporary_access_grants_under_watch.return_value = [
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

        api_client.list_temporary_access_grants_under_watch.return_value = [
            _granting(granted_at=datetime.now(UTC) - timedelta(minutes=61))
        ]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.CANCELLED, DeployOutcome.CANCELLED, id="deploy-grant-1"
        )

        await supervise_temporary_access(api_client, redis_client)

        api_client.withdraw_deploy_dispatch.assert_not_awaited()
        assert _published_deploy(redis_client).env_overrides == {ENV_KEY: ""}


def _revoking(**overrides) -> TemporaryAccessGrantDTO:
    """A grant whose revoke deploy is out and whose server has not answered yet."""
    fields = {
        "status": TemporaryAccessStatus.REVOKING,
        "revoke_run_id": "deploy-revoke-1",
        "revoke_reason": TemporaryAccessRevokeReason.RUN_TERMINAL,
        "revoke_attempts": 1,
    }
    fields.update(overrides)
    return _make_grant(**fields)


def _posted(api_client) -> list[TemporaryAccessObservation]:
    """Every reading the sweep handed to the record."""
    return [call.args[1] for call in api_client.record_temporary_access_observation.call_args_list]


class TestRevocationIsObserved:
    """A grant is closed by what the server shows, not by a deploy that reported success.

    Between the sweep and the deployed service stands GitHub Actions, which is
    asynchronous and not ours. So "revoked" is a reading of the environment the
    service is actually running with, taken more than once, and everything short
    of that leaves the grant live and the sweep working.
    """

    @pytest.mark.asyncio
    async def test_a_deploy_that_reported_success_does_not_close_the_grant_on_its_own(
        self, api_client, redis_client
    ):
        """Nothing has been read yet, so nothing is settled — and the reading is asked for."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [_revoking()]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.COMPLETED, DeployOutcome.SUCCESS
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["revoked"] == 0
        assert _grant_updates(api_client) == []
        assert _posted(api_client) == []

        asked = _published(redis_client, ENV_OBSERVATION_QUEUE)
        assert len(asked) == 1
        assert isinstance(asked[0], EnvObservationRequest)
        assert asked[0].request_id == _question()
        assert asked[0].env_key == ENV_KEY
        assert asked[0].server_handle == "vps-1"
        assert asked[0].service_slug == "palindrome-bot"

    @pytest.mark.asyncio
    async def test_the_server_read_is_the_one_the_qa_run_was_tested_on(
        self, api_client, redis_client
    ):
        """A project can run on several servers; only one of them ran the tested bot.

        The reviewed hole: reading whichever deployment came back first let an
        empty slot on an unrelated server close a grant whose bot still admitted
        the test identity. The handoff the grant carries names the application,
        so that is the one read.
        """
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.get_application_if_missing_returns_none.return_value = _application(
            application_id=42, server_handle="vps-the-bot-was-tested-on"
        )
        api_client.list_temporary_access_grants_under_watch.return_value = [_revoking()]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.COMPLETED, DeployOutcome.SUCCESS
        )

        await supervise_temporary_access(api_client, redis_client)

        api_client.get_application_if_missing_returns_none.assert_awaited_with(42)
        asked = _published(redis_client, ENV_OBSERVATION_QUEUE)
        assert asked[0].server_handle == "vps-the-bot-was-tested-on"

    @pytest.mark.asyncio
    async def test_an_application_outside_the_project_is_not_read_at_all(
        self, api_client, redis_client
    ):
        """A reading of somebody else's deployment is not evidence about this grant."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.get_application_if_missing_returns_none.return_value = _application(
            repo_id="repo-of-another-project"
        )
        api_client.list_temporary_access_grants_under_watch.return_value = [_revoking()]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.COMPLETED, DeployOutcome.SUCCESS
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["revoke_failed"] == 1
        assert _published(redis_client, ENV_OBSERVATION_QUEUE) == []
        assert _posted(api_client) == []
        assert _grant_updates(api_client) == []

    @pytest.mark.asyncio
    async def test_one_clear_reading_does_not_end_reconciliation(self, api_client, redis_client):
        """The reviewed hole: a single empty reading closed the grant for good.

        A dispatch already on its way to GitHub Actions lands after a reading was
        taken. So the record keeps the grant live, the sweep keeps reading, and a
        value that appears afterwards is still somebody's problem here.
        """
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [_revoking()]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.COMPLETED, DeployOutcome.SUCCESS
        )
        redis_client.redis.answer(_observed(present=False))
        api_client.record_temporary_access_observation.return_value = _under_confirmation()

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts == _no_action()
        reading = _posted(api_client)[-1]
        assert reading.present is False
        assert reading.application_id == 42
        assert reading.observation_id == _question()

    @pytest.mark.asyncio
    async def test_the_grant_closes_once_the_record_says_the_readings_agree(
        self, api_client, redis_client
    ):
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [
            _revoking(
                slot_clear_readings=1, slot_clear_since=datetime.now(UTC) - timedelta(hours=1)
            )
        ]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.COMPLETED, DeployOutcome.SUCCESS
        )
        redis_client.redis.answer(_observed(present=False, request_id=_question(readings=1)))
        api_client.record_temporary_access_observation.return_value = _confirmed_revoked()

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["revoked"] == 1
        # Nothing writes the status by hand; the record decided from the reading.
        assert _grant_updates(api_client) == []
        assert _published(redis_client, DEPLOY_QUEUE) == []

    @pytest.mark.asyncio
    async def test_a_slot_that_still_holds_the_value_keeps_the_grant_revoking(
        self, api_client, redis_client
    ):
        """The card's case: the revoke is out, the reading disagrees, so it goes out again.

        This is also what a writer that arrived late looks like from here — a
        value on the server that should not be there. The sweep does not care
        which it was; it revokes again either way.
        """
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [_revoking()]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.COMPLETED, DeployOutcome.SUCCESS
        )
        redis_client.redis.answer(_observed(present=True))
        api_client.record_temporary_access_observation.return_value = _slot_still_filled()

        first = await supervise_temporary_access(api_client, redis_client)

        assert first["revoked"] == 0
        assert first["revoke_failed"] == 1
        held = _grant_updates(api_client)[-1]
        assert held.status is TemporaryAccessStatus.REVOKE_FAILED
        assert ENV_KEY in held.last_error

        # And the next sweep, reading the grant back, revokes again.
        api_client.list_temporary_access_grants_under_watch.return_value = [
            _make_grant(
                status=TemporaryAccessStatus.REVOKE_FAILED,
                revoke_run_id="deploy-revoke-1",
                revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
                revoke_attempts=1,
                last_error=held.last_error,
            )
        ]
        second = await supervise_temporary_access(api_client, redis_client)

        assert second["dispatched"] == 1
        assert _published_deploy(redis_client).env_overrides == {ENV_KEY: ""}

    @pytest.mark.asyncio
    async def test_a_value_that_comes_back_after_a_clear_reading_is_caught(
        self, api_client, redis_client
    ):
        """A late writer during the confirmation window is a disagreement, and it is fixed.

        The first reading found the slot empty. The grant stayed live, so the
        second reading has something to disagree with — which is the whole point
        of not closing on the first one.
        """
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [
            _revoking(
                slot_clear_readings=1,
                slot_clear_since=datetime.now(UTC) - timedelta(minutes=20),
                observed_at=datetime.now(UTC) - timedelta(minutes=20),
            )
        ]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.COMPLETED, DeployOutcome.SUCCESS
        )
        redis_client.redis.answer(_observed(present=True, request_id=_question(readings=1)))
        api_client.record_temporary_access_observation.return_value = _slot_still_filled()

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["revoked"] == 0
        assert counts["revoke_failed"] == 1
        assert _grant_updates(api_client)[-1].status is TemporaryAccessStatus.REVOKE_FAILED

    @pytest.mark.asyncio
    async def test_an_unreadable_server_is_neither_a_revocation_nor_a_failure(
        self, api_client, redis_client
    ):
        """A channel that is down says nothing about the access, so it settles nothing."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [_revoking()]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.COMPLETED, DeployOutcome.SUCCESS
        )
        redis_client.redis.answer(_unreachable())

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts == _no_action()
        assert _grant_updates(api_client) == []
        assert _posted(api_client) == []
        # The silence is taken rather than left to be re-read; the marker stays,
        # so asking again is paced instead of repeated every tick.
        assert env_observation_result_key(_question()) not in redis_client.redis.values

    @pytest.mark.asyncio
    async def test_an_application_that_is_not_running_is_not_an_empty_slot(
        self, api_client, redis_client
    ):
        """There is no environment to read, which is not the same as reading an empty one."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.get_application_if_missing_returns_none.return_value = _application(
            ApplicationStatus.STOPPED
        )
        api_client.list_temporary_access_grants_under_watch.return_value = [_revoking()]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.COMPLETED, DeployOutcome.SUCCESS
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts == _no_action()
        assert _grant_updates(api_client) == []
        assert _published(redis_client, ENV_OBSERVATION_QUEUE) == []

    @pytest.mark.asyncio
    async def test_the_same_question_is_asked_once_per_window(self, api_client, redis_client):
        """The sweep runs far more often than a playbook takes."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [_revoking()]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.COMPLETED, DeployOutcome.SUCCESS
        )

        await supervise_temporary_access(api_client, redis_client)
        await supervise_temporary_access(api_client, redis_client)

        assert len(_published(redis_client, ENV_OBSERVATION_QUEUE)) == 1
        assert env_observation_pending_key(_question()) in redis_client.redis.values

    @pytest.mark.asyncio
    async def test_the_next_reading_waits_for_the_window_to_pass(self, api_client, redis_client):
        """A reading just taken is not re-taken on the very next tick."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [
            _revoking(
                slot_clear_readings=1,
                slot_clear_since=datetime.now(UTC),
                observed_at=datetime.now(UTC),
            )
        ]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.COMPLETED, DeployOutcome.SUCCESS
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts == _no_action()
        assert _published(redis_client, ENV_OBSERVATION_QUEUE) == []

    @pytest.mark.asyncio
    async def test_a_new_revoke_attempt_asks_its_own_question(self, api_client, redis_client):
        """An answer about the previous attempt cannot settle this one."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [
            _revoking(revoke_run_id="deploy-revoke-2", revoke_attempts=2)
        ]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.COMPLETED, DeployOutcome.SUCCESS, id="deploy-revoke-2"
        )
        # The first attempt's reading, which said the value was gone.
        redis_client.redis.answer(_observed(present=False))

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts["revoked"] == 0
        assert _posted(api_client) == []
        asked = _published(redis_client, ENV_OBSERVATION_QUEUE)
        assert [request.request_id for request in asked] == [_question("deploy-revoke-2")]

    @pytest.mark.asyncio
    async def test_a_disagreement_that_outlives_the_grant_is_handed_to_a_human(
        self, api_client, redis_client, monkeypatch
    ):
        """Retrying forever is not an outcome, so the run says what is being observed."""
        from src.tasks import temporary_access as module

        monkeypatch.setattr(module, "notify_admins_best_effort", AsyncMock())
        api_client.list_temporary_access_grants_under_watch.return_value = [
            _revoking(granted_at=datetime.now(UTC) - timedelta(minutes=121))
        ]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.COMPLETED, DeployOutcome.SUCCESS
        )
        redis_client.redis.answer(_observed(present=True))
        api_client.record_temporary_access_observation.return_value = _slot_still_filled()

        await module.supervise_temporary_access(api_client, redis_client)

        _, escalation = _escalation(api_client)
        blocker = escalation["run_result"].blocker
        assert blocker.category is QABlockerCategory.QA_CLEANUP_FAILED
        assert ENV_KEY in blocker.received
        assert "still carries" in blocker.received


def _closed(**overrides) -> TemporaryAccessGrantDTO:
    """A grant the readings have closed, still inside the watch window."""
    fields = {
        "status": TemporaryAccessStatus.REVOKED,
        "revoke_run_id": "deploy-revoke-1",
        "revoke_reason": TemporaryAccessRevokeReason.RUN_TERMINAL,
        "revoke_attempts": 1,
        "revoked_at": datetime.now(UTC) - timedelta(minutes=20),
        "observed_at": datetime.now(UTC) - timedelta(minutes=20),
        "observation_id": _question(readings=2),
        "slot_clear_since": datetime.now(UTC) - timedelta(minutes=40),
        "slot_clear_readings": REVOKE_CONFIRMATION_READINGS,
    }
    fields.update(overrides)
    return _make_grant(**fields)


def _reopened(**overrides) -> TemporaryAccessGrantDTO:
    """The record's answer to a reading that found the value on a closed grant."""
    fields = {
        "status": TemporaryAccessStatus.REVOKING,
        "revoke_run_id": "deploy-revoke-1",
        "revoke_reason": TemporaryAccessRevokeReason.OBSERVED_AFTER_REVOKE,
        "revoke_attempts": 0,
        "revoked_at": None,
        "reopened_at": datetime.now(UTC),
        "observed_at": datetime.now(UTC),
        "observation_id": _question(readings=REVOKE_CONFIRMATION_READINGS),
        "slot_clear_since": None,
        "slot_clear_readings": 0,
        "last_error": f"{ENV_KEY} is set on application 42 after the grant was confirmed revoked",
    }
    fields.update(overrides)
    return _make_grant(**fields)


class TestAValueThatComesBackAfterTheGrantClosed:
    """The reviewed hole: closing the grant stopped anybody looking at the slot.

    The record closes a grant on readings, and readings are moments. The same
    writer that can land between two of them can land after the last one — a
    dispatch GitHub Actions had already accepted, or a hand-run deploy. So the
    slot is read for a while after the grant closed, and a value found there is
    taken off again.
    """

    @pytest.mark.asyncio
    async def test_a_closed_grant_is_still_read(self, api_client, redis_client):
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [_closed()]

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts == _no_action()
        asked = _published(redis_client, ENV_OBSERVATION_QUEUE)
        assert [request.request_id for request in asked] == [
            _question(readings=REVOKE_CONFIRMATION_READINGS)
        ]
        # Nothing was decided from the deploy runs; the slot itself is the question.
        assert _published(redis_client, DEPLOY_QUEUE) == []

    @pytest.mark.asyncio
    async def test_a_value_applied_after_the_grant_closed_is_revoked_by_the_next_sweep(
        self, api_client, redis_client, monkeypatch
    ):
        """The card's promise: access does not outlive the cycle that sees it."""
        from src.tasks import temporary_access as module

        monkeypatch.setattr(module, "notify_admins_best_effort", AsyncMock())
        api_client.list_temporary_access_grants_under_watch.return_value = [_closed()]
        api_client.record_temporary_access_observation.return_value = _reopened()
        redis_client.redis.answer(
            _observed(present=True, request_id=_question(readings=REVOKE_CONFIRMATION_READINGS))
        )

        counts = await module.supervise_temporary_access(api_client, redis_client)

        assert counts["dispatched"] == 1
        deploy = _published_deploy(redis_client)
        assert deploy.env_overrides == {ENV_KEY: ""}
        assert deploy.head_sha == HEAD_SHA
        reopened = _grant_updates(api_client)[-1]
        assert reopened.status is TemporaryAccessStatus.REVOKING
        assert reopened.revoke_reason is TemporaryAccessRevokeReason.OBSERVED_AFTER_REVOKE
        # The returned value gets its own attempts rather than inheriting a
        # budget the first episode already spent.
        assert reopened.revoke_attempts == 1

    @pytest.mark.asyncio
    async def test_a_closed_grant_whose_slot_reads_empty_is_left_alone(
        self, api_client, redis_client
    ):
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [_closed()]
        api_client.record_temporary_access_observation.return_value = _closed(
            slot_clear_readings=REVOKE_CONFIRMATION_READINGS + 1,
            observed_at=datetime.now(UTC),
            observation_id=_question(readings=REVOKE_CONFIRMATION_READINGS),
        )
        redis_client.redis.answer(
            _observed(present=False, request_id=_question(readings=REVOKE_CONFIRMATION_READINGS))
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts == _no_action()
        assert _published(redis_client, DEPLOY_QUEUE) == []
        assert _grant_updates(api_client) == []

    @pytest.mark.asyncio
    async def test_a_value_the_record_will_not_reopen_is_left_to_its_owner(
        self, api_client, redis_client
    ):
        """A later grant may hold the same slot on purpose.

        The contract has one slot per key, so what is being read can be the next
        grant's value rather than this one's leftover. The record refuses to
        reopen then, and revoking from here would take the live grant's access
        off under it.
        """
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [_closed()]
        api_client.record_temporary_access_observation.return_value = _closed(
            observed_at=datetime.now(UTC),
            observation_id=_question(readings=REVOKE_CONFIRMATION_READINGS),
        )
        redis_client.redis.answer(
            _observed(present=True, request_id=_question(readings=REVOKE_CONFIRMATION_READINGS))
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts == _no_action()
        assert _published(redis_client, DEPLOY_QUEUE) == []

    @pytest.mark.asyncio
    async def test_the_watch_is_asked_for_by_the_window_it_covers(self, api_client, redis_client):
        """The sweep names how far back a closed grant is still worth reading."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = []

        await supervise_temporary_access(api_client, redis_client)

        cutoff = api_client.list_temporary_access_grants_under_watch.call_args.args[0]
        assert 59 <= (datetime.now(UTC) - cutoff).total_seconds() / 60 <= 61

    @pytest.mark.asyncio
    async def test_an_unreadable_server_leaves_a_closed_grant_closed(
        self, api_client, redis_client
    ):
        """Silence is not a reading, so it neither reopens the grant nor clears it."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [
            # Old enough that a live grant would be reported as unrevokable by
            # now. A closed one has nothing to report: it is being watched, not
            # waited on, and the record refuses to be written anyway.
            _closed(granted_at=datetime.now(UTC) - timedelta(minutes=121))
        ]
        redis_client.redis.answer(
            _unreachable(request_id=_question(readings=REVOKE_CONFIRMATION_READINGS))
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts == _no_action()
        assert _posted(api_client) == []
        assert _published(redis_client, DEPLOY_QUEUE) == []
        assert _grant_updates(api_client) == []


def _long_closed(**overrides) -> TemporaryAccessGrantDTO:
    """A grant closed long enough ago that the cooling-off watch is over.

    Nothing is looking at its slot on the fast cadence any more. What still
    holds is the contract: the key is empty while no grant owns it, and the slow
    check is what reads that.
    """
    long_ago = datetime.now(UTC) - timedelta(days=9)
    fields = {
        "granted_at": long_ago,
        "revoked_at": long_ago,
        "observed_at": long_ago,
    }
    fields.update(overrides)
    return _closed(**fields)


class TestTheSlowCheckOfTheContractSlot:
    """The reviewed hole: the promise used to end when the fast watch did.

    The writer the guarantee is about is GitHub Actions and whatever else can
    reach the server, and neither is bounded by a 60-minute window. A value
    applied at minute 61 fell out of the watch and stood for good: nothing read
    the slot again, so no revoke, no visible failure and no human.

    So the promise is made twice. The fast level, paced in minutes, is unchanged
    and covers the dispatch that was already in flight. The slow level, paced in
    hours, checks the invariant itself for as long as the slot exists — the key
    is empty while no grant holds it — and hands what it finds to the same
    revoke and the same escalation.
    """

    @pytest.mark.asyncio
    async def test_a_value_restored_after_the_watch_expired_is_still_taken_off(
        self, api_client, redis_client, monkeypatch
    ):
        """The regression the review asked for, at the far side of the window."""
        from src.tasks import temporary_access as module

        monkeypatch.setattr(module, "notify_admins_best_effort", AsyncMock())
        api_client.list_temporary_access_grants_under_watch.return_value = [_long_closed()]
        api_client.record_temporary_access_observation.return_value = _reopened()
        redis_client.redis.answer(
            _observed(present=True, request_id=_question(readings=REVOKE_CONFIRMATION_READINGS))
        )

        counts = await module.supervise_temporary_access(api_client, redis_client)

        assert counts["dispatched"] == 1
        deploy = _published_deploy(redis_client)
        assert deploy.env_overrides == {ENV_KEY: ""}
        assert deploy.head_sha == HEAD_SHA
        reopened = _grant_updates(api_client)[-1]
        assert reopened.status is TemporaryAccessStatus.REVOKING
        assert reopened.revoke_reason is TemporaryAccessRevokeReason.OBSERVED_AFTER_REVOKE

    @pytest.mark.asyncio
    async def test_a_slot_nobody_watches_is_asked_for_by_its_own_cadence(
        self, api_client, redis_client
    ):
        """The sweep names both cutoffs: minutes for the watch, hours for the check."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = []

        await supervise_temporary_access(api_client, redis_client)

        watch_from, audit_before = (
            api_client.list_temporary_access_grants_under_watch.call_args.args
        )
        assert 59 <= (datetime.now(UTC) - watch_from).total_seconds() / 60 <= 61
        assert 23.9 <= (datetime.now(UTC) - audit_before).total_seconds() / 3600 <= 24.1

    @pytest.mark.asyncio
    async def test_a_server_that_cannot_be_read_costs_one_playbook_per_interval(
        self, api_client, redis_client
    ):
        """An ssh per project is what makes this cadence hours rather than minutes.

        A slot is due until something reads it, and a machine that is down is
        never read. Without a marker of its own the slow check would ask on every
        tick for as long as the machine stayed down, which is the cost the
        cadence exists to avoid.
        """
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [_long_closed()]

        await supervise_temporary_access(api_client, redis_client)
        await supervise_temporary_access(api_client, redis_client)

        assert len(_published(redis_client, ENV_OBSERVATION_QUEUE)) == 1

    @pytest.mark.asyncio
    async def test_a_slot_with_nothing_left_to_read_is_not_worked_on_every_tick(
        self, api_client, redis_client
    ):
        """A slot whose application is gone costs the same one attempt as any other.

        Nothing can be read there, so nothing ever stamps it and it stays due for
        good. Deciding that afresh on every tick would be a round of lookups per
        tick for every project that ever held a grant, so the interval is taken
        before the lookups rather than before the question.
        """
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [_long_closed()]
        api_client.get_application_if_missing_returns_none.return_value = None

        await supervise_temporary_access(api_client, redis_client)
        await supervise_temporary_access(api_client, redis_client)

        assert api_client.get_application_if_missing_returns_none.await_count == 1
        assert _published(redis_client, ENV_OBSERVATION_QUEUE) == []

    @pytest.mark.asyncio
    async def test_the_fast_watch_still_reads_every_window(self, api_client, redis_client):
        """The slow marker must not slow the level it does not belong to.

        A grant closed a moment ago is where a dispatch already in flight lands,
        and that is read on the observation window. Only a slot past the watch is
        the slow check's.
        """
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [
            _closed(observed_at=datetime.now(UTC) - timedelta(minutes=6))
        ]

        await supervise_temporary_access(api_client, redis_client)
        redis_client.redis.values.pop(
            env_observation_pending_key(_question(readings=REVOKE_CONFIRMATION_READINGS))
        )
        await supervise_temporary_access(api_client, redis_client)

        assert len(_published(redis_client, ENV_OBSERVATION_QUEUE)) == 2

    @pytest.mark.asyncio
    async def test_a_slot_the_slow_check_reads_empty_is_left_alone(self, api_client, redis_client):
        """The invariant holding is the ordinary answer and costs nothing else."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [_long_closed()]
        api_client.record_temporary_access_observation.return_value = _long_closed(
            observed_at=datetime.now(UTC),
            slot_clear_readings=REVOKE_CONFIRMATION_READINGS + 1,
        )
        redis_client.redis.answer(
            _observed(present=False, request_id=_question(readings=REVOKE_CONFIRMATION_READINGS))
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts == _no_action()
        assert _published(redis_client, DEPLOY_QUEUE) == []
        assert _grant_updates(api_client) == []

    @pytest.mark.asyncio
    async def test_an_unreadable_server_leaves_the_slow_check_undecided(
        self, api_client, redis_client
    ):
        """Silence is not a reading here either: no revoke, no failure, no human."""
        from src.tasks.temporary_access import supervise_temporary_access

        api_client.list_temporary_access_grants_under_watch.return_value = [_long_closed()]
        redis_client.redis.answer(
            _unreachable(request_id=_question(readings=REVOKE_CONFIRMATION_READINGS))
        )

        counts = await supervise_temporary_access(api_client, redis_client)

        assert counts == _no_action()
        assert _posted(api_client) == []
        assert _published(redis_client, DEPLOY_QUEUE) == []
        assert _grant_updates(api_client) == []

    @pytest.mark.asyncio
    async def test_a_value_the_slow_check_cannot_remove_reaches_a_human(
        self, api_client, redis_client, monkeypatch
    ):
        """A discrepancy found late fails as visibly as one found early.

        The reopening gives the disagreement its own budget, and once that is
        spent the QA run carries the named cleanup failure and the story goes to
        a person instead of waiting in TESTING.
        """
        from src.tasks import temporary_access as module

        monkeypatch.setattr(module, "notify_admins_best_effort", AsyncMock())
        spent = _reopened(
            reopened_at=datetime.now(UTC) - timedelta(minutes=121),
            revoke_attempts=3,
        )
        api_client.list_temporary_access_grants_under_watch.return_value = [spent]
        api_client.get_run_if_missing_returns_none.return_value = _deploy_run(
            RunStatus.COMPLETED, DeployOutcome.SUCCESS
        )
        redis_client.redis.answer(_observed(present=True))
        api_client.record_temporary_access_observation.return_value = spent

        await module.supervise_temporary_access(api_client, redis_client)

        _, escalation = _escalation(api_client)
        blocker = escalation["run_result"].blocker
        assert blocker.category is QABlockerCategory.QA_CLEANUP_FAILED
        assert ENV_KEY in blocker.received
        assert "still carries" in blocker.received
