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
from shared.contracts.queues.qa import QAMessage, QAOutcome
from shared.queues import DEPLOY_QUEUE

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
    "supervisor.temporary_access_observation_window_minutes": 5,
    "supervisor.temporary_access_unrevoked_ttl_minutes": 0,
}


@pytest.fixture
async def config(api_client):
    """Load the sweep's operational constants the way the service loads them."""
    from src import startup

    for key, value in _CONFIG.items():
        await api_client._request(
            "POST",
            "system-configs/",
            json={"key": key, "value": value, "category": "supervisor"},
        )
    store = ConfigStore(api_client.base_url, cache_ttl=0)
    previous = startup.config
    startup.config = store
    yield store
    startup.config = previous


@pytest.fixture
def redis_client():
    """Collects what the sweep publishes without needing a broker."""

    class _Collector:
        def __init__(self):
            self.published: list[tuple[str, object]] = []

        async def publish_message(self, queue, message):
            self.published.append((queue, message))

        def deploys(self, project_id: str):
            # The sweep is global by design: it settles every live grant it
            # finds, including ones other tests left behind. Only this test's
            # project is its subject.
            return [
                m for q, m in self.published if q == DEPLOY_QUEUE and m.project_id == project_id
            ]

    return _Collector()


async def _project(api_client) -> str:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = str(uuid.uuid4())
    await api_client._request(
        "POST",
        "users/",
        json={"telegram_id": telegram_id, "username": f"sweep_{telegram_id}"},
    )
    await api_client._request(
        "POST",
        "projects/",
        json={"id": project_id, "title": "Temporary Access Sweep", "config": {}},
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    return project_id


async def _run(api_client, project_id: str, run_id: str, run_type: str) -> str:
    await api_client.create_run({"id": run_id, "type": run_type, "project_id": project_id})
    return run_id


async def _grant(api_client, project_id: str, qa_run_id: str, grant_run_id: str):
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
                user_id="",
                deployed_url="https://example.com",
                application_id=42,
                acceptance_criteria="the bot answers /start",
                run_id=qa_run_id,
            ),
        )
    )


@pytest.mark.asyncio
async def test_a_passed_qa_run_still_ends_in_a_named_cleanup_failure(
    api_client, redis_client, config
):
    """Exhausted revokes on a run that already passed.

    The QA worker finished and recorded `passed`; only afterwards did the
    revokes run out. Reading the run back must not find `passed` next to a bot
    that still admits the test identity — the run says why cleanup failed, and
    the grant carries the stamp that lets the story stop waiting.
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
    assert run.status is RunStatus.FAILED
    assert run.result.qa_outcome is QAOutcome.BLOCKED
    assert run.result.blocker.category is QABlockerCategory.QA_CLEANUP_FAILED

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
    reclaim = await api_client._request("POST", f"runs/{grant_run_id}/dispatch-claim")
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
    await api_client._request("POST", f"runs/{grant_run_id}/dispatch-claim")

    with patch("src.tasks.temporary_access.notify_admins_best_effort", AsyncMock()):
        await supervise_temporary_access(api_client, redis_client)

    assert redis_client.deploys(project_id) == []
    held = await api_client.get_live_temporary_access_grant_for_run(qa_run_id)
    assert held.status is TemporaryAccessStatus.GRANTING


async def _claim_and_expire(api_client, run_id: str) -> dict:
    """Claim the boundary, then let the lease run out the way waiting would."""
    claimed = await api_client._request("POST", f"runs/{run_id}/dispatch-claim")
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
