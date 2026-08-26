"""Service-level proofs for atomic count-based work admission."""

import asyncio
from http import HTTPStatus
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import EngineeringBudgetReservation, Run, SystemConfig, WorkAdmissionAudit


async def _set_config(db_session: AsyncSession, key: str, value: int | bool) -> None:
    config = await db_session.get(SystemConfig, key)
    assert config is not None
    config.value = value
    await db_session.commit()


@pytest.mark.asyncio
async def test_concurrent_project_creations_cannot_pass_the_same_user_ceiling(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    """The per-user row lock admits only one request when one slot remains."""
    telegram_id = 830_000_001
    created = await async_client.post(
        "/api/users/", json={"telegram_id": telegram_id, "username": "admission-race"}
    )
    assert created.status_code == HTTPStatus.CREATED, created.text

    await _set_config(db_session, "work_admission.max_projects_per_user", 1)
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
                    "message": "Достигнут лимит активных проектов. Попробуйте позже.",
                }
            return response.status_code

        statuses = await asyncio.gather(create_project(), create_project())
        assert sorted(statuses) == [HTTPStatus.CREATED, HTTPStatus.CONFLICT]
    finally:
        await _set_config(db_session, "work_admission.max_projects_per_user", 10000)


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

    replay = await async_client.post(
        "/api/work-admission/paid-runs",
        json={"id": canonical_id, "type": "qa", "project_id": project_id},
    )
    assert replay.status_code == HTTPStatus.OK, replay.text
    assert replay.json()["run_id"] == canonical_id
    assert (await db_session.scalars(select(Run.id).where(Run.id == canonical_id))).all() == [
        canonical_id
    ]

    conflict = await async_client.post(
        "/api/work-admission/paid-runs",
        json={
            "id": canonical_id,
            "type": "qa",
            "project_id": project_id,
            "run_metadata": {"different": True},
        },
    )
    assert conflict.status_code == HTTPStatus.CONFLICT
    assert conflict.json()["detail"]["code"] == "paid_run_command_conflict"


@pytest.mark.asyncio
async def test_emergency_stop_requires_strict_bool_and_rejects_generic_mutation(
    async_client: AsyncClient,
):
    invalid = await async_client.put(
        "/api/work-admission/emergency-stop", json={"enabled": "false"}
    )
    assert invalid.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    protected = await async_client.patch(
        "/api/system-configs/work_admission.emergency_stop", json={"value": "false"}
    )
    assert protected.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.asyncio
async def test_paid_run_refusal_is_durable_but_the_same_command_rechecks_the_stop(
    async_client: AsyncClient, db_session: AsyncSession
):
    telegram_id = 830_000_004
    project_id = str(uuid.uuid4())
    user = await async_client.post(
        "/api/users/", json={"telegram_id": telegram_id, "username": "admission-owner"}
    )
    assert user.status_code == HTTPStatus.CREATED
    assert (
        await async_client.post(
            "/api/projects/",
            headers={"X-Telegram-ID": str(telegram_id)},
            json={
                "id": project_id,
                "title": "Admission owner",
                "initiating_run_id": str(uuid.uuid4()),
                "status": "active",
                "config": {},
            },
        )
    ).status_code == HTTPStatus.CREATED
    run_id = f"qa-refused-{uuid.uuid4().hex}"
    await _set_config(db_session, "work_admission.emergency_stop", True)
    try:
        payload = {"id": run_id, "type": "qa", "project_id": project_id}
        refused = await async_client.post("/api/work-admission/paid-runs", json=payload)
        assert refused.status_code == HTTPStatus.OK, refused.text
        assert refused.json()["admission"] == {
            "outcome": "denied",
            "reason": "emergency_stop",
            "retryable": False,
            "message": "Запуск новой работы временно остановлен оператором.",
        }
        replay = await async_client.post("/api/work-admission/paid-runs", json=payload)
        assert replay.json() == refused.json()
        audit = await db_session.scalar(
            select(WorkAdmissionAudit).where(WorkAdmissionAudit.reference_id == run_id)
        )
        assert audit is not None
        assert audit.user_id == user.json()["id"]
        assert audit.reason == "emergency_stop"
        assert audit.message == "Запуск новой работы временно остановлен оператором."
        assert audit.command_payload == {
            **payload,
            "story_id": None,
            "task_id": None,
            "run_metadata": {},
            "callback_stream": None,
        }
        assert await db_session.scalar(select(Run).where(Run.id == run_id)) is None
        await _set_config(db_session, "work_admission.emergency_stop", False)
        restarted = await async_client.post("/api/work-admission/paid-runs", json=payload)
        assert restarted.status_code == HTTPStatus.OK, restarted.text
        assert restarted.json()["run_id"] == run_id
        run = await db_session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None and run.status == "queued"
    finally:
        await _set_config(db_session, "work_admission.emergency_stop", False)


@pytest.mark.asyncio
async def test_concurrent_paid_run_starts_hold_the_last_slot(
    async_client: AsyncClient, db_session: AsyncSession
):
    """Two real HTTP commands released at one barrier cannot overbook a paid slot."""
    telegram_id = 830_000_003
    project_id = str(uuid.uuid4())
    assert (
        await async_client.post(
            "/api/users/", json={"telegram_id": telegram_id, "username": "paid-run-race"}
        )
    ).status_code == HTTPStatus.CREATED
    assert (
        await async_client.post(
            "/api/projects/",
            headers={"X-Telegram-ID": str(telegram_id)},
            json={
                "id": project_id,
                "title": "Paid Run Race",
                "initiating_run_id": str(uuid.uuid4()),
                "status": "active",
                "config": {},
            },
        )
    ).status_code == HTTPStatus.CREATED

    occupied_id = f"qa-occupied-{uuid.uuid4().hex}"
    db_session.add(
        Run(id=occupied_id, type="qa", status="queued", project_id=project_id, run_metadata={})
    )
    await db_session.commit()
    occupied_count = int(
        await db_session.scalar(
            select(func.count())
            .select_from(Run)
            .where(Run.type.in_(("engineering", "qa")), Run.status.in_(("queued", "running")))
        )
        or 0
    )
    # The service database is shared by the suite.  Its existing paid runs are
    # already occupied slots; this test supplies exactly one additional N-1 slot.
    await _set_config(db_session, "work_admission.max_concurrent_paid_runs", occupied_count + 1)
    barrier = asyncio.Barrier(2)
    try:

        async def start() -> dict:
            await barrier.wait()
            response = await async_client.post(
                "/api/work-admission/paid-runs",
                json={"id": f"qa-race-{uuid.uuid4().hex}", "type": "qa", "project_id": project_id},
            )
            assert response.status_code == HTTPStatus.OK, response.text
            return response.json()

        first, second = await asyncio.gather(start(), start())
        assert sorted((first["admission"]["outcome"], second["admission"]["outcome"])) == [
            "admitted",
            "deferred",
        ]
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(Run)
                .where(Run.type.in_(("engineering", "qa")), Run.status.in_(("queued", "running")))
            )
        ) == occupied_count + 1
    finally:
        await _set_config(db_session, "work_admission.max_concurrent_paid_runs", 10000)


@pytest.mark.asyncio
async def test_same_paid_command_rechecks_a_released_capacity_slot(
    async_client: AsyncClient, db_session: AsyncSession
):
    telegram_id = 830_000_005
    project_id = str(uuid.uuid4())
    assert (
        await async_client.post(
            "/api/users/", json={"telegram_id": telegram_id, "username": "paid-retry"}
        )
    ).status_code == HTTPStatus.CREATED
    assert (
        await async_client.post(
            "/api/projects/",
            headers={"X-Telegram-ID": str(telegram_id)},
            json={
                "id": project_id,
                "title": "Paid retry",
                "initiating_run_id": str(uuid.uuid4()),
                "status": "active",
                "config": {},
            },
        )
    ).status_code == HTTPStatus.CREATED
    occupied = Run(
        id=f"qa-capacity-{uuid.uuid4().hex}",
        type="qa",
        status="queued",
        project_id=project_id,
        run_metadata={},
    )
    db_session.add(occupied)
    await db_session.commit()
    occupied_count = int(
        await db_session.scalar(
            select(func.count())
            .select_from(Run)
            .where(Run.type.in_(("engineering", "qa")), Run.status.in_(("queued", "running")))
        )
        or 0
    )
    await _set_config(db_session, "work_admission.max_concurrent_paid_runs", occupied_count)
    try:
        payload = {"id": f"qa-retry-{uuid.uuid4().hex}", "type": "qa", "project_id": project_id}
        refused = await async_client.post("/api/work-admission/paid-runs", json=payload)
        assert refused.json()["admission"]["reason"] == "paid_work_limit"
        occupied.status = "completed"
        await db_session.commit()
        admitted = await async_client.post("/api/work-admission/paid-runs", json=payload)
        assert admitted.status_code == HTTPStatus.OK, admitted.text
        assert admitted.json()["run_id"] == payload["id"]
    finally:
        await _set_config(db_session, "work_admission.max_concurrent_paid_runs", 10000)
