"""A settled QA run's unverified checks land on its project, and the admin API reads them back."""

import uuid

from fastapi import status
from httpx import AsyncClient
import pytest

WITHHELD = {
    "name": "criterion not verifiable by QA: - POST /api/transactions returns 201",
    "reason": "needs an HTTP write",
    "origin": "withheld",
}
EXECUTOR = {"name": "upload receipt", "reason": "no tool to upload", "origin": "executor"}


async def _qa_run(async_client: AsyncClient) -> tuple[str, str, int]:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = str(uuid.uuid4())
    user = await async_client.post(
        "/api/users/",
        json={"telegram_id": telegram_id, "username": f"gaps_{telegram_id}", "is_admin": True},
    )
    assert user.status_code == status.HTTP_201_CREATED
    project = await async_client.post(
        "/api/projects/",
        json={
            "initiating_run_id": "test-run-1",
            "id": project_id,
            "title": "Verification Gaps",
            "config": {},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert project.status_code == status.HTTP_201_CREATED
    run_id = f"qa-{uuid.uuid4().hex[:8]}"
    run = await async_client.post(
        "/api/work-admission/paid-runs",
        json={"id": run_id, "type": "qa", "project_id": project_id},
    )
    assert run.status_code == status.HTTP_200_OK
    return project_id, run_id, telegram_id


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["passed", "failed"])
async def test_a_settled_runs_gaps_are_written_once_and_read_back(
    async_client: AsyncClient, outcome: str
):
    project_id, run_id, admin_id = await _qa_run(async_client)
    settled = await async_client.patch(
        f"/api/runs/{run_id}",
        json={
            "status": "completed",
            "result": {
                "qa_outcome": outcome,
                "passed_checks": ["GET /health returns 200"],
                "unverified_checks": [WITHHELD, EXECUTOR],
            },
        },
    )
    assert settled.status_code == status.HTTP_200_OK

    first = await async_client.post(
        f"/api/projects/{project_id}/verification-gaps/from-run", json={"run_id": run_id}
    )
    again = await async_client.post(
        f"/api/projects/{project_id}/verification-gaps/from-run", json={"run_id": run_id}
    )

    assert first.status_code == status.HTTP_200_OK
    assert first.json() == {"recorded": [WITHHELD["name"], EXECUTOR["name"]], "already_recorded": 0}
    assert again.json() == {"recorded": [], "already_recorded": 2}
    listed = await async_client.get(
        f"/api/projects/{project_id}/verification-gaps", headers={"X-Telegram-ID": str(admin_id)}
    )
    assert listed.status_code == status.HTTP_200_OK
    gaps = listed.json()
    assert [(gap["name"], gap["reason"], gap["origin"]) for gap in gaps] == [
        (WITHHELD["name"], WITHHELD["reason"], "withheld"),
        (EXECUTOR["name"], EXECUTOR["reason"], "executor"),
    ]
    assert {gap["run_id"] for gap in gaps} == {run_id}
    assert all(gap["project_id"] == project_id and gap["created_at"] for gap in gaps)


@pytest.mark.asyncio
async def test_a_blocked_run_records_no_gaps(async_client: AsyncClient):
    project_id, run_id, _ = await _qa_run(async_client)
    await async_client.patch(
        f"/api/runs/{run_id}",
        json={
            "status": "completed",
            "result": {
                "qa_outcome": "blocked",
                "blocker": {"category": "unknown", "attempted": "a", "sent": "s", "received": "r"},
                "unverified_checks": [WITHHELD],
            },
        },
    )

    refused = await async_client.post(
        f"/api/projects/{project_id}/verification-gaps/from-run", json={"run_id": run_id}
    )

    assert refused.status_code == status.HTTP_409_CONFLICT
    assert (await async_client.get(f"/api/projects/{project_id}/verification-gaps")).json() == []


@pytest.mark.asyncio
async def test_a_deleted_project_takes_its_gaps_with_it(async_client: AsyncClient):
    project_id, run_id, _ = await _qa_run(async_client)
    await async_client.patch(
        f"/api/runs/{run_id}",
        json={
            "status": "completed",
            "result": {"qa_outcome": "passed", "unverified_checks": [WITHHELD]},
        },
    )
    await async_client.post(
        f"/api/projects/{project_id}/verification-gaps/from-run", json={"run_id": run_id}
    )

    deleted = await async_client.delete(f"/api/projects/{project_id}")

    assert deleted.status_code == status.HTTP_204_NO_CONTENT, deleted.text
