"""The sweep settles a grant against a real API, with nothing held in memory.

Every test here builds a durable state through the API, runs one sweep, and
reads back what the API says afterwards. No test hands the sweep a handle to
anything: the process that granted the access is gone by construction, which is
the whole claim the card makes — revocation follows from the record, not from
the tail of a happy path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
import uuid

import pytest

from shared.config_store import ConfigStore
from shared.contracts.dto.deploy_dispatch import (
    DISPATCH_LEASE_EXPIRES_AT_KEY,
    DISPATCH_SUPERSEDED_AT_KEY,
)
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.run_result import QABlockerCategory
from shared.contracts.dto.temporary_access import (
    TemporaryAccessGrantCreate,
    TemporaryAccessStatus,
)
from shared.contracts.queues.env_observation import (
    EnvObservationOutcome,
    EnvObservationResult,
    env_observation_result_key,
)
from shared.contracts.queues.qa import QAMessage, QAOutcome
from shared.queues import DEPLOY_QUEUE, ENV_OBSERVATION_QUEUE

HEAD_SHA = "c" * 40
ENV_KEY = "TG_BOT_TEST_TELEGRAM_ID"

# A zero lifetime and a zero staleness bound put the sweep straight into the
# decisions this module is about, without a test that waits an hour. Nothing
# here depends on the real minutes; what is under test is what the sweep does
# once a bound has passed.
_MAX_REVOKE_ATTEMPTS = 3
_CONFIG = {
    "supervisor.temporary_access_ttl_minutes": 0,
    "supervisor.temporary_access_revoke_stale_minutes": 0,
    "supervisor.temporary_access_max_revoke_attempts": _MAX_REVOKE_ATTEMPTS,
    # Zero here too: the pacing between readings is not what these tests are
    # about, and waiting one out would only make them slow.
    "supervisor.temporary_access_observation_window_minutes": 0,
    "supervisor.temporary_access_unrevoked_ttl_minutes": 0,
    # How long a closed grant keeps being read. Not zero: these tests must see
    # the sweep still asking about a slot the record has already closed.
    "supervisor.temporary_access_revoked_watch_minutes": 60,
    "supervisor.temporary_access_contract_audit_hours": 24,
}


@pytest.fixture
async def config(api_client):
    """Load the sweep's operational constants the way the service loads them."""
    from src import startup

    for key, value in _CONFIG.items():
        await api_client.request(
            "POST",
            "system-configs/",
            json={"key": key, "value": value, "category": "supervisor"},
        )
    store = ConfigStore(api_client.base_url, cache_ttl=0)
    previous = startup.config
    startup.config = store
    yield store
    startup.config = previous


class _Keys:
    """The little bit of Redis the sweep carries its question and answer in."""

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


@pytest.fixture
def redis_client():
    """Collects what the sweep publishes without needing a broker."""

    class _Collector:
        def __init__(self):
            self.published: list[tuple[str, object]] = []
            self.redis = _Keys()

        async def publish_message(self, queue, message):
            self.published.append((queue, message))

        def deploys(self, project_id: str):
            # The sweep is global by design: it settles every live grant it
            # finds, including ones other tests left behind. Only this test's
            # project is its subject.
            return [
                m for q, m in self.published if q == DEPLOY_QUEUE and m.project_id == project_id
            ]

        def observations(self, project_id: str):
            return [
                m
                for q, m in self.published
                if q == ENV_OBSERVATION_QUEUE and m.project_id == project_id
            ]

        def answer(self, request_id: str, result: EnvObservationResult) -> None:
            """Leave the answer the reader on the server would have left."""
            self.redis.values[env_observation_result_key(request_id)] = result.model_dump_json()

    return _Collector()


async def _project(api_client) -> str:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = str(uuid.uuid4())
    await api_client.request(
        "POST",
        "users/",
        json={"telegram_id": telegram_id, "username": f"sweep_{telegram_id}"},
    )
    await api_client.request(
        "POST",
        "projects/",
        json={"id": project_id, "title": "Temporary Access Sweep", "config": {}},
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    return project_id


async def _run(api_client, project_id: str, run_id: str, run_type: str) -> str:
    await api_client.create_run({"id": run_id, "type": run_type, "project_id": project_id})
    return run_id


async def _grant(
    api_client,
    project_id: str,
    qa_run_id: str,
    grant_run_id: str,
    application_id: int = 42,
):
    return await api_client.create_temporary_access_grant(
        TemporaryAccessGrantCreate(
            id=f"tempaccess-{qa_run_id}",
            project_id=project_id,
            env_key=ENV_KEY,
            subject="424242",
            head_sha=HEAD_SHA,
            qa_run_id=qa_run_id,
            grant_run_id=grant_run_id,
            qa_message=QAMessage(
                story_id="story-1",
                project_id=project_id,
                initiating_run_id="live-run-1",
                telegram_chat_id="",
                deployed_url="https://example.com",
                # The deployment QA tested the borrowed identity on. The reading
                # that closes this grant has to be of that machine and no other.
                application_id=application_id,
                acceptance_criteria="the bot answers /start",
                run_id=qa_run_id,
            ),
        )
    )


@pytest.mark.asyncio
async def test_a_passed_qa_run_keeps_its_verdict_when_cleanup_escalates(
    api_client, redis_client, config
):
    """Exhausted revokes on a run that already passed.

    The QA worker finished and recorded `passed`; only afterwards did the
    revokes run out. The grant carries the escalation and administrator alert,
    but cannot replace the QA run's first terminal outcome.
    """
    from src.tasks.temporary_access import supervise_temporary_access

    project_id = await _project(api_client)
    qa_run_id = await _run(api_client, project_id, f"qa-{uuid.uuid4().hex[:8]}", "qa")
    revoke_run_id = await _run(
        api_client, project_id, f"deploy-revoke-{uuid.uuid4().hex[:8]}", "deploy"
    )
    grant = await _grant(api_client, project_id, qa_run_id, f"deploy-grant-{uuid.uuid4().hex[:8]}")

    # The worker's own verdict, recorded and frozen before any of this.
    await api_client.update_run(
        qa_run_id,
        {
            "status": RunStatus.COMPLETED.value,
            "result": {"qa_outcome": QAOutcome.PASSED.value, "summary": "the bot answered"},
        },
    )
    # The last revoke attempt, failed.
    await api_client.update_run(
        revoke_run_id,
        {"status": RunStatus.FAILED.value, "result": {"deploy_outcome": "give_up"}},
    )
    await api_client.update_temporary_access_grant(
        grant.id,
        _revoking(revoke_run_id, attempts=_MAX_REVOKE_ATTEMPTS),
    )

    await supervise_temporary_access(api_client, redis_client)

    run = await api_client.get_run(qa_run_id)
    assert run.status is RunStatus.COMPLETED
    assert run.result.qa_outcome is QAOutcome.PASSED

    settled = await api_client.get_live_temporary_access_grant_for_run(qa_run_id)
    assert settled is not None
    assert settled.escalated_at is not None


@pytest.mark.asyncio
async def test_a_grant_deploy_whose_worker_never_returns_is_revoked_anyway(
    api_client, redis_client, config
):
    """The process-death case the card is about, end to end.

    A worker claimed the dispatch boundary and died. Nothing in this test holds
    a handle to it, and nothing will ever record its outcome. The sweep starts
    from the stored grant alone: it takes the expired claim back and dispatches
    a revoke that clears the value, fencing whatever the dead worker may have
    left running on Actions.
    """
    from src.tasks.temporary_access import supervise_temporary_access

    project_id = await _project(api_client)
    qa_run_id = await _run(api_client, project_id, f"qa-{uuid.uuid4().hex[:8]}", "qa")
    grant_run_id = await _run(
        api_client, project_id, f"deploy-grant-{uuid.uuid4().hex[:8]}", "deploy"
    )
    await _grant(api_client, project_id, qa_run_id, grant_run_id)

    claim = await _claim_and_expire(api_client, grant_run_id)
    assert claim["granted"] is True

    await supervise_temporary_access(api_client, redis_client)

    deploys = redis_client.deploys(project_id)
    assert len(deploys) == 1
    assert deploys[0].env_overrides == {ENV_KEY: ""}
    assert deploys[0].fence_active_deploys is True

    dead = await api_client.get_run(grant_run_id)
    assert dead.status is RunStatus.CANCELLED
    assert dead.run_metadata[DISPATCH_SUPERSEDED_AT_KEY]
    # And the claim is closed: the worker cannot come back and dispatch.
    reclaim = await api_client.request("POST", f"runs/{grant_run_id}/dispatch-claim")
    assert reclaim.json()["granted"] is False


@pytest.mark.asyncio
async def test_a_claim_inside_its_lease_is_waited_for(api_client, redis_client, config):
    """The other side of the same rule: a worker that may still dispatch is not overtaken.

    Clearing the value now would find no Actions run to fence, record the grant
    revoked, and let that deploy write the identity back afterwards.
    """
    from src.tasks.temporary_access import supervise_temporary_access

    project_id = await _project(api_client)
    qa_run_id = await _run(api_client, project_id, f"qa-{uuid.uuid4().hex[:8]}", "qa")
    grant_run_id = await _run(
        api_client, project_id, f"deploy-grant-{uuid.uuid4().hex[:8]}", "deploy"
    )
    await _grant(api_client, project_id, qa_run_id, grant_run_id)
    await api_client.request("POST", f"runs/{grant_run_id}/dispatch-claim")

    with patch("src.tasks.temporary_access.notify_admins_best_effort", AsyncMock()):
        await supervise_temporary_access(api_client, redis_client)

    assert redis_client.deploys(project_id) == []
    held = await api_client.get_live_temporary_access_grant_for_run(qa_run_id)
    assert held.status is TemporaryAccessStatus.GRANTING


@pytest.mark.asyncio
async def test_a_successful_revoke_deploy_is_not_yet_a_revocation(api_client, redis_client, config):
    """The deploy reported success and nothing has been read, so nothing is settled.

    This is the whole change of criterion, seen against a real record: the grant
    stays live in REVOKING and the sweep asks the server what the service is
    actually running with.
    """
    from src.tasks.temporary_access import supervise_temporary_access

    project_id, application_id = await _deployed_project(api_client)
    qa_run_id, revoke_run_id = await _grant_being_revoked(api_client, project_id, application_id)

    await supervise_temporary_access(api_client, redis_client)

    held = await api_client.get_live_temporary_access_grant_for_run(qa_run_id)
    assert held is not None
    assert held.status is TemporaryAccessStatus.REVOKING

    asked = redis_client.observations(project_id)
    assert [request.request_id for request in asked] == [_question(revoke_run_id)]
    assert asked[0].env_key == ENV_KEY


@pytest.mark.asyncio
async def test_a_reading_that_still_shows_the_value_reaches_a_human(
    api_client, redis_client, config
):
    """A disagreement past the grant's lifetime is an outcome, not another retry.

    A value on the server after a revoke that succeeded is the same thing as a
    late writer putting it back. Here it has outlived what the config allows, so
    the QA run says what is being observed and the story stops waiting.
    """
    from src.tasks.temporary_access import supervise_temporary_access

    project_id, application_id = await _deployed_project(api_client)
    qa_run_id, revoke_run_id = await _grant_being_revoked(api_client, project_id, application_id)
    redis_client.answer(_question(revoke_run_id), _observed(_question(revoke_run_id), present=True))

    with patch("src.tasks.temporary_access.notify_admins_best_effort", AsyncMock()):
        await supervise_temporary_access(api_client, redis_client)

    run = await api_client.get_run(qa_run_id)
    assert run.status is RunStatus.FAILED
    assert run.result.blocker.category is QABlockerCategory.QA_CLEANUP_FAILED
    assert ENV_KEY in run.result.blocker.received

    still_out = await api_client.get_live_temporary_access_grant_for_run(qa_run_id)
    assert still_out is not None, "an observed value must not leave the grant settled"
    assert still_out.escalated_at is not None


@pytest.mark.asyncio
async def test_one_empty_reading_does_not_end_the_reconciliation(api_client, redis_client, config):
    """The reviewed hole: a single empty reading closed the grant for good.

    A dispatch already on its way to GitHub Actions lands after a reading was
    taken, so a grant closed on the first empty one has nothing left watching it.
    The record keeps it live, the sweep asks a fresh question, and a value that
    comes back is somebody's problem again.
    """
    from src.tasks.temporary_access import supervise_temporary_access

    project_id, application_id = await _deployed_project(api_client)
    qa_run_id, revoke_run_id = await _grant_being_revoked(api_client, project_id, application_id)
    redis_client.answer(
        _question(revoke_run_id), _observed(_question(revoke_run_id), present=False)
    )

    await supervise_temporary_access(api_client, redis_client)

    held = await api_client.get_live_temporary_access_grant_for_run(qa_run_id)
    assert held is not None, "one reading is a moment, not a confirmed state"
    assert held.status is TemporaryAccessStatus.REVOKING
    assert held.slot_clear_readings == 1
    assert held.observation_id == _question(revoke_run_id)

    # And the next sweep asks its own question rather than re-reading that one.
    await supervise_temporary_access(api_client, redis_client)
    assert [request.request_id for request in redis_client.observations(project_id)] == [
        _question(revoke_run_id, readings=1)
    ]


@pytest.mark.asyncio
async def test_a_value_that_comes_back_after_an_empty_reading_is_revoked_again(
    api_client, redis_client, config
):
    """The late writer the guarantee is written against, against a real record.

    The first reading found the slot empty. Because that did not close the grant,
    the second reading has something to disagree with, and the disagreement is
    acted on.
    """
    from src.tasks.temporary_access import supervise_temporary_access

    project_id, application_id = await _deployed_project(api_client)
    qa_run_id, revoke_run_id = await _grant_being_revoked(api_client, project_id, application_id)
    redis_client.answer(
        _question(revoke_run_id), _observed(_question(revoke_run_id), present=False)
    )
    await supervise_temporary_access(api_client, redis_client)

    # Something applied the old value after the empty reading was taken.
    redis_client.answer(
        _question(revoke_run_id, readings=1),
        _observed(_question(revoke_run_id, readings=1), present=True),
    )
    with patch("src.tasks.temporary_access.notify_admins_best_effort", AsyncMock()):
        await supervise_temporary_access(api_client, redis_client)

    caught = await api_client.get_live_temporary_access_grant_for_run(qa_run_id)
    assert caught is not None
    assert caught.slot_clear_readings == 0
    assert ENV_KEY in caught.last_error


def _question(revoke_run_id: str, readings: int = 0) -> str:
    """The id of one reading: which revoke attempt asked, and which reading it is."""
    return f"envobs-{revoke_run_id}-{readings}"


def _observed(request_id: str, *, present: bool) -> EnvObservationResult:
    return EnvObservationResult(
        request_id=request_id,
        outcome=EnvObservationOutcome.OBSERVED,
        env_key=ENV_KEY,
        present=present,
        containers=2,
    )


async def _deployed_project(api_client) -> tuple[str, int]:
    """A project with something running, which is what can be read back."""
    project_id = await _project(api_client)
    handle = f"vps-{uuid.uuid4().hex[:8]}"
    await api_client.request(
        "POST",
        "servers/",
        json={"handle": handle, "host": f"{handle}.example.com", "public_ip": "10.9.9.9"},
    )
    repo = await api_client.request(
        "POST",
        "repositories/",
        json={
            "project_id": project_id,
            "name": f"repo-{project_id[:8]}",
            "git_url": f"https://github.com/test-org/repo-{project_id[:8]}.git",
        },
    )
    application = await api_client.request(
        "POST",
        "applications/",
        json={
            "repo_id": repo.json()["id"],
            "server_handle": handle,
            "service_name": "temporary-access-bot",
            "status": "running",
        },
    )
    return project_id, application.json()["id"]


async def _grant_being_revoked(api_client, project_id: str, application_id: int) -> tuple[str, str]:
    """A grant whose revoke deploy is done and reported success."""
    qa_run_id = await _run(api_client, project_id, f"qa-{uuid.uuid4().hex[:8]}", "qa")
    revoke_run_id = await _run(
        api_client, project_id, f"deploy-revoke-{uuid.uuid4().hex[:8]}", "deploy"
    )
    grant = await _grant(
        api_client,
        project_id,
        qa_run_id,
        f"deploy-grant-{uuid.uuid4().hex[:8]}",
        application_id=application_id,
    )
    await api_client.update_run(
        revoke_run_id,
        {"status": RunStatus.COMPLETED.value, "result": {"deploy_outcome": "success"}},
    )
    await api_client.update_temporary_access_grant(grant.id, _revoking(revoke_run_id, attempts=1))
    return qa_run_id, revoke_run_id


async def _claim_and_expire(api_client, run_id: str) -> dict:
    """Claim the boundary, then let the lease run out the way waiting would."""
    claimed = await api_client.request("POST", f"runs/{run_id}/dispatch-claim")
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await api_client.update_run(run_id, {"run_metadata": {DISPATCH_LEASE_EXPIRES_AT_KEY: past}})
    return claimed.json()


def _revoking(revoke_run_id: str, *, attempts: int):
    from shared.contracts.dto.temporary_access import (
        TemporaryAccessGrantUpdate,
        TemporaryAccessRevokeReason,
    )

    return TemporaryAccessGrantUpdate(
        status=TemporaryAccessStatus.REVOKING,
        revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
        revoke_run_id=revoke_run_id,
        revoke_attempts=attempts,
    )
