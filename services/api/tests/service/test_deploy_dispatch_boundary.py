"""Claiming and withdrawing a deploy run's dispatch are one decision, not two.

A worker about to reach GitHub and a revoke trying to stop it before it does are
racing for the same answer. Whoever wins, the loser has to be told which side of
the boundary the deploy ended up on: a deploy that never dispatched can be
treated as gone, while one that did has to be stopped on GitHub Actions instead.

Holding the boundary is a lease rather than a possession. A worker that claims
it and then dies would otherwise leave a run nothing can settle and a revoke
waiting on it for good, so the claim has a deadline the holder promises not to
dispatch past, and reconciliation can take it back once that has gone by.
"""

from datetime import UTC, datetime, timedelta
import uuid

from fastapi import status
from httpx import AsyncClient
import pytest

from shared.contracts.dto.deploy_dispatch import (
    DISPATCH_LEASE_EXPIRES_AT_KEY,
    DISPATCH_SUPERSEDED_AT_KEY,
)


async def _deploy_run(async_client: AsyncClient, *, run_status: str = "running") -> str:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = str(uuid.uuid4())

    user = await async_client.post(
        "/api/users/",
        json={"telegram_id": telegram_id, "username": f"dispatch_{telegram_id}"},
    )
    assert user.status_code == status.HTTP_201_CREATED

    project = await async_client.post(
        "/api/projects/",
        json={"id": project_id, "title": "Dispatch Boundary", "config": {}},
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert project.status_code == status.HTTP_201_CREATED

    run_id = f"deploy-grant-{uuid.uuid4().hex[:8]}"
    run = await async_client.post(
        "/api/runs/",
        json={"id": run_id, "type": "deploy", "project_id": project_id},
    )
    assert run.status_code == status.HTTP_201_CREATED

    # A run is born running; RunCreate carries no status, so anything else has
    # to be reached the way the system reaches it, with a patch.
    if run.json()["status"] != run_status:
        patched = await async_client.patch(f"/api/runs/{run_id}", json={"status": run_status})
        assert patched.status_code == status.HTTP_200_OK
        assert patched.json()["status"] == run_status
    return run_id


@pytest.mark.asyncio
async def test_a_cancelled_run_cannot_be_started(async_client: AsyncClient):
    """The interleaving the worker's own read cannot cover.

    A worker reads a live run, the sweep withdraws it, and the worker then takes
    it to running. If that write lands, the dispatch claim below sees a run that
    is not terminal and grants it: the revoke has already cleared the value and
    recorded the grant revoked, and this deploy writes the identity back.
    """
    run_id = await _deploy_run(async_client)

    await async_client.post(f"/api/runs/{run_id}/dispatch-withdraw")
    started = await async_client.post(f"/api/runs/{run_id}/start")

    assert started.status_code == status.HTTP_200_OK
    assert started.json()["started"] is False
    assert started.json()["run_status"] == "cancelled"

    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["status"] == "cancelled"
    assert run.json()["completed_at"] is not None
    claimed = await async_client.post(f"/api/runs/{run_id}/dispatch-claim")
    assert claimed.json()["granted"] is False


@pytest.mark.asyncio
async def test_a_plain_patch_cannot_resurrect_a_cancelled_run(async_client: AsyncClient):
    """Not only the deploy worker's path: no writer may undo a terminal state."""
    run_id = await _deploy_run(async_client)

    await async_client.post(f"/api/runs/{run_id}/dispatch-withdraw")
    patched = await async_client.patch(f"/api/runs/{run_id}", json={"status": "running"})

    assert patched.status_code == status.HTTP_409_CONFLICT
    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_starting_a_live_run_twice_is_the_same_answer(async_client: AsyncClient):
    """A worker retrying after a lost response must not be refused its own start."""
    run_id = await _deploy_run(async_client, run_status="queued")

    first = await async_client.post(f"/api/runs/{run_id}/start")
    second = await async_client.post(f"/api/runs/{run_id}/start")

    assert first.json()["started"] is True
    assert second.json()["started"] is True
    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["status"] == "running"


@pytest.mark.asyncio
async def test_a_live_run_may_claim_the_dispatch(async_client: AsyncClient):
    run_id = await _deploy_run(async_client)

    claimed = await async_client.post(f"/api/runs/{run_id}/dispatch-claim")

    assert claimed.status_code == status.HTTP_200_OK
    assert claimed.json()["granted"] is True
    assert claimed.json()["claimed_at"] is not None


@pytest.mark.asyncio
async def test_claiming_twice_is_the_same_answer(async_client: AsyncClient):
    """A worker retrying after a lost response must not be refused its own claim."""
    run_id = await _deploy_run(async_client)

    first = await async_client.post(f"/api/runs/{run_id}/dispatch-claim")
    second = await async_client.post(f"/api/runs/{run_id}/dispatch-claim")

    assert second.json()["granted"] is True
    assert second.json()["claimed_at"] == first.json()["claimed_at"]


@pytest.mark.asyncio
async def test_a_withdrawal_before_the_claim_stops_the_deploy(async_client: AsyncClient):
    """The refusal is what keeps a cancelled run from reaching GitHub at all."""
    run_id = await _deploy_run(async_client)

    withdrawn = await async_client.post(
        f"/api/runs/{run_id}/dispatch-withdraw", params={"reason": "grant abandoned"}
    )
    claimed = await async_client.post(f"/api/runs/{run_id}/dispatch-claim")

    assert withdrawn.json()["outcome"] == "withdrawn"
    assert claimed.json()["granted"] is False
    assert claimed.json()["run_status"] == "cancelled"

    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["status"] == "cancelled"
    assert run.json()["error_message"] == "grant abandoned"


@pytest.mark.asyncio
async def test_a_withdrawal_after_the_claim_says_the_deploy_is_already_out(
    async_client: AsyncClient,
):
    """The caller has to stop it on GitHub Actions, not assume it never started."""
    run_id = await _deploy_run(async_client)

    await async_client.post(f"/api/runs/{run_id}/dispatch-claim")
    withdrawn = await async_client.post(f"/api/runs/{run_id}/dispatch-withdraw")

    assert withdrawn.json()["outcome"] == "already_dispatched"
    assert withdrawn.json()["claimed_at"] is not None
    # Still cancelled: the worker polls that and stops its own Actions run.
    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_withdrawing_a_finished_run_is_not_an_error(async_client: AsyncClient):
    """A repeated stop on something already stopped is the expected retry."""
    run_id = await _deploy_run(async_client, run_status="completed")

    withdrawn = await async_client.post(f"/api/runs/{run_id}/dispatch-withdraw")

    assert withdrawn.status_code == status.HTTP_200_OK
    assert withdrawn.json()["outcome"] == "already_terminal"
    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["status"] == "completed"


async def _expire_lease(async_client: AsyncClient, run_id: str) -> None:
    """Move the claim's deadline into the past, the way waiting would."""
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    patched = await async_client.patch(
        f"/api/runs/{run_id}",
        json={"run_metadata": {DISPATCH_LEASE_EXPIRES_AT_KEY: past}},
    )
    assert patched.status_code == status.HTTP_200_OK


@pytest.mark.asyncio
async def test_a_claim_carries_a_deadline(async_client: AsyncClient):
    """Holding the boundary is a lease. Without a deadline nothing outside can
    ever tell a worker that is about to dispatch from one that never will."""
    run_id = await _deploy_run(async_client)

    claimed = await async_client.post(f"/api/runs/{run_id}/dispatch-claim")

    lease = datetime.fromisoformat(claimed.json()["lease_expires_at"])
    assert lease > datetime.now(UTC)


@pytest.mark.asyncio
async def test_claiming_again_renews_the_deadline_without_moving_the_crossing(
    async_client: AsyncClient,
):
    """A worker that asks again is alive, so it gets more time — but the moment
    the deploy first crossed is a fact and does not move."""
    run_id = await _deploy_run(async_client)

    first = await async_client.post(f"/api/runs/{run_id}/dispatch-claim")
    await _expire_lease(async_client, run_id)
    second = await async_client.post(f"/api/runs/{run_id}/dispatch-claim")

    assert second.json()["claimed_at"] == first.json()["claimed_at"]
    assert datetime.fromisoformat(second.json()["lease_expires_at"]) > datetime.now(UTC)


@pytest.mark.asyncio
async def test_a_live_lease_is_waited_for_rather_than_taken_back(async_client: AsyncClient):
    """The claimer may still be on its way to GitHub, and only it knows."""
    run_id = await _deploy_run(async_client)

    await async_client.post(f"/api/runs/{run_id}/dispatch-claim")
    superseded = await async_client.post(f"/api/runs/{run_id}/dispatch-supersede")

    assert superseded.json()["outcome"] == "lease_live"
    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["status"] == "running"


@pytest.mark.asyncio
async def test_an_expired_claim_is_taken_back_and_can_never_dispatch(async_client: AsyncClient):
    """The process-death case, from the API's side.

    A worker that claimed and never came back leaves a run nothing can settle,
    and everything waiting on it waits for good. Past the deadline the claim is
    taken back: the run is cancelled, so the holder cannot re-claim, and the
    crossing is stamped so a restarted reader sees it without asking again.
    """
    run_id = await _deploy_run(async_client)

    await async_client.post(f"/api/runs/{run_id}/dispatch-claim")
    await _expire_lease(async_client, run_id)
    superseded = await async_client.post(
        f"/api/runs/{run_id}/dispatch-supersede", params={"reason": "grant abandoned"}
    )

    assert superseded.json()["outcome"] == "superseded"
    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["status"] == "cancelled"
    assert run.json()["error_message"] == "grant abandoned"
    assert run.json()["run_metadata"][DISPATCH_SUPERSEDED_AT_KEY]
    assert run.json()["completed_at"] is not None

    reclaimed = await async_client.post(f"/api/runs/{run_id}/dispatch-claim")
    assert reclaimed.json()["granted"] is False


@pytest.mark.asyncio
async def test_superseding_twice_is_the_same_answer(async_client: AsyncClient):
    """The sweep repeats after a lost response; that must not be an error."""
    run_id = await _deploy_run(async_client)

    await async_client.post(f"/api/runs/{run_id}/dispatch-claim")
    await _expire_lease(async_client, run_id)
    first = await async_client.post(f"/api/runs/{run_id}/dispatch-supersede")
    second = await async_client.post(f"/api/runs/{run_id}/dispatch-supersede")

    assert first.json()["outcome"] == "superseded"
    assert second.json()["outcome"] == "superseded"


@pytest.mark.asyncio
async def test_a_worker_that_recorded_its_outcome_keeps_it(async_client: AsyncClient):
    """Its own account of the deploy is the better answer and is not overwritten."""
    run_id = await _deploy_run(async_client)

    await async_client.post(f"/api/runs/{run_id}/dispatch-claim")
    await _expire_lease(async_client, run_id)
    recorded = await async_client.patch(
        f"/api/runs/{run_id}",
        json={"status": "cancelled", "result": {"deploy_outcome": "cancelled"}},
    )
    assert recorded.status_code == status.HTTP_200_OK

    superseded = await async_client.post(f"/api/runs/{run_id}/dispatch-supersede")

    assert superseded.json()["outcome"] == "already_settled"
    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["result"]["deploy_outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_a_run_nobody_claimed_has_nothing_to_take_back(async_client: AsyncClient):
    """Nothing crossed, so there is no external effect to account for."""
    run_id = await _deploy_run(async_client)

    superseded = await async_client.post(f"/api/runs/{run_id}/dispatch-supersede")

    assert superseded.json()["outcome"] == "not_claimed"
    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["status"] == "running"


@pytest.mark.asyncio
async def test_a_superseded_claim_still_lets_its_worker_say_what_it_did(
    async_client: AsyncClient,
):
    """Taking the wait back is not a verdict on the deploy.

    If the worker turns out to be alive, its own result is still the account of
    what happened outside, and it has to be able to write it.
    """
    run_id = await _deploy_run(async_client)

    await async_client.post(f"/api/runs/{run_id}/dispatch-claim")
    await _expire_lease(async_client, run_id)
    await async_client.post(f"/api/runs/{run_id}/dispatch-supersede")

    recorded = await async_client.patch(
        f"/api/runs/{run_id}",
        json={"status": "cancelled", "result": {"deploy_outcome": "cancelled"}},
    )

    assert recorded.status_code == status.HTTP_200_OK
    assert recorded.json()["result"]["deploy_outcome"] == "cancelled"
