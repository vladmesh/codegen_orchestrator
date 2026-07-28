"""Claiming and withdrawing a deploy run's dispatch are one decision, not two.

A worker about to reach GitHub and a revoke trying to stop it before it does are
racing for the same answer. Whoever wins, the loser has to be told which side of
the boundary the deploy ended up on: a deploy that never dispatched can be
treated as gone, while one that did has to be stopped on GitHub Actions instead.
"""

import uuid

from fastapi import status
from httpx import AsyncClient
import pytest


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
        json={
            "id": run_id,
            "type": "deploy",
            "project_id": project_id,
            "status": run_status,
        },
    )
    assert run.status_code == status.HTTP_201_CREATED
    return run_id


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
