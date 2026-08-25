"""Service-level proofs for atomic count-based work admission."""

import asyncio
from http import HTTPStatus
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import EngineeringBudgetReservation, Run


async def _set_config(client: AsyncClient, key: str, value: int | bool) -> None:
    response = await client.post(
        "/api/system-configs/",
        json={
            "key": key,
            "value": value,
            "category": "work_admission",
            "description": "Service test override",
            "updated_by": "test",
        },
    )
    assert response.status_code == HTTPStatus.CREATED, response.text


@pytest.mark.asyncio
async def test_concurrent_project_creations_cannot_pass_the_same_user_ceiling(
    async_client: AsyncClient,
):
    """The per-user row lock admits only one request when one slot remains."""
    telegram_id = 830_000_001
    created = await async_client.post(
        "/api/users/", json={"telegram_id": telegram_id, "username": "admission-race"}
    )
    assert created.status_code == HTTPStatus.CREATED, created.text

    await _set_config(async_client, "work_admission.max_projects_per_user", 1)
    try:

        async def create_project() -> int:
            response = await async_client.post(
                "/api/projects/",
                headers={"X-Telegram-ID": str(telegram_id)},
                json={
                    "id": str(uuid.uuid4()),
                    "title": "Concurrent admission",
                    "initiating_run_id": str(uuid.uuid4()),
                    "status": "active",
                    "config": {},
                },
            )
            if response.status_code == HTTPStatus.CONFLICT:
                assert response.json()["detail"]["admission"] == {
                    "outcome": "denied",
                    "reason": "project_limit",
                    "retryable": False,
                }
            return response.status_code

        statuses = await asyncio.gather(create_project(), create_project())
        assert sorted(statuses) == [HTTPStatus.CREATED, HTTPStatus.CONFLICT]
    finally:
        await _set_config(async_client, "work_admission.max_projects_per_user", 10000)


@pytest.mark.asyncio
async def test_generic_runs_reject_paid_types_while_canonical_start_creates_qa_run(
    async_client: AsyncClient, db_session: AsyncSession
):
    """The paid-work boundary rejects bypasses but admits its canonical command."""
    telegram_id = 830_000_002
    project_id = str(uuid.uuid4())
    user = await async_client.post(
        "/api/users/", json={"telegram_id": telegram_id, "username": "paid-run-boundary"}
    )
    assert user.status_code == HTTPStatus.CREATED, user.text
    project = await async_client.post(
        "/api/projects/",
        headers={"X-Telegram-ID": str(telegram_id)},
        json={
            "id": project_id,
            "title": "Paid Run Boundary",
            "initiating_run_id": str(uuid.uuid4()),
            "status": "active",
            "config": {},
        },
    )
    assert project.status_code == HTTPStatus.CREATED, project.text

    engineering_id = f"engineering-bypass-{uuid.uuid4().hex}"
    qa_id = f"qa-bypass-{uuid.uuid4().hex}"
    for run_id, run_type in ((engineering_id, "engineering"), (qa_id, "qa")):
        refused = await async_client.post(
            "/api/runs/", json={"id": run_id, "type": run_type, "project_id": project_id}
        )
        assert refused.status_code == HTTPStatus.CONFLICT, refused.text

    assert (
        await db_session.scalars(select(Run.id).where(Run.id.in_((engineering_id, qa_id))))
    ).all() == []
    assert (
        await db_session.scalars(
            select(EngineeringBudgetReservation.id).where(
                EngineeringBudgetReservation.attempt_id == engineering_id
            )
        )
    ).all() == []

    canonical_id = f"qa-canonical-{uuid.uuid4().hex}"
    started = await async_client.post(
        "/api/work-admission/paid-runs",
        json={"id": canonical_id, "type": "qa", "project_id": project_id},
    )
    assert started.status_code == HTTPStatus.OK, started.text
    assert started.json()["run_id"] == canonical_id
    assert await db_session.scalar(select(Run).where(Run.id == canonical_id)) is not None
