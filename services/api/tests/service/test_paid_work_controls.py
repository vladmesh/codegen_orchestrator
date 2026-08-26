"""Service-level proofs for typed paid-work operator controls."""

from http import HTTPStatus
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import SystemConfig, WorkAdmissionAudit

_DEFAULTS = {
    "work_admission.emergency_stop": False,
    "work_admission.max_projects_per_user": 10000,
    "work_admission.max_concurrent_paid_runs": 10000,
    "work_admission.engineering_executor_override": "none",
    "work_admission.qa_executor_override": "none",
}


async def _ensure_controls(db: AsyncSession) -> None:
    for key, value in _DEFAULTS.items():
        if await db.get(SystemConfig, key) is None:
            db.add(
                SystemConfig(
                    key=key,
                    value=value,
                    category="work_admission",
                    description="test control",
                )
            )
    await db.commit()


@pytest.mark.asyncio
async def test_typed_paid_work_controls_are_complete_strict_and_audited(
    async_client: AsyncClient, db_session: AsyncSession
):
    await _ensure_controls(db_session)

    current = await async_client.get("/api/work-admission/controls")
    assert current.status_code == HTTPStatus.OK, current.text
    assert current.json() == {
        "emergency_stop": False,
        "max_concurrent_paid_runs": 10000,
        "engineering_executor_override": "none",
        "qa_executor_override": "none",
    }

    partial = await async_client.put(
        "/api/work-admission/controls", json={"engineering_executor_override": "claude"}
    )
    assert partial.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    invalid = await async_client.put(
        "/api/work-admission/controls",
        json={
            "emergency_stop": False,
            "max_concurrent_paid_runs": True,
            "engineering_executor_override": "factory",
            "qa_executor_override": "none",
        },
    )
    assert invalid.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    changed = await async_client.put(
        "/api/work-admission/controls",
        json={
            "emergency_stop": False,
            "max_concurrent_paid_runs": 7,
            "engineering_executor_override": "claude",
            "qa_executor_override": "codex",
        },
    )
    assert changed.status_code == HTTPStatus.OK, changed.text
    assert changed.json()["engineering_executor_override"] == "claude"
    audits = (
        await db_session.scalars(
            select(WorkAdmissionAudit).where(WorkAdmissionAudit.subject == "paid_work_control")
        )
    ).all()
    assert {(audit.control_name, audit.before_value, audit.after_value) for audit in audits} == {
        ("max_concurrent_paid_runs", 10000, 7),
        ("engineering_executor_override", "none", "claude"),
        ("qa_executor_override", "none", "codex"),
    }
    assert all(
        audit.actor == "internal_service" and audit.created_at is not None for audit in audits
    )


@pytest.mark.asyncio
async def test_paid_runs_persist_the_global_override_and_legacy_reset(
    async_client: AsyncClient, db_session: AsyncSession
):
    await _ensure_controls(db_session)
    controls = {
        "emergency_stop": False,
        "max_concurrent_paid_runs": 10000,
        "engineering_executor_override": "claude",
        "qa_executor_override": "codex",
    }
    assert (
        await async_client.put("/api/work-admission/controls", json=controls)
    ).status_code == HTTPStatus.OK

    telegram_id = 831_000_001
    project_id = str(uuid.uuid4())
    assert (
        await async_client.post(
            "/api/users/", json={"telegram_id": telegram_id, "username": "override-owner"}
        )
    ).status_code == HTTPStatus.CREATED
    assert (
        await async_client.post(
            "/api/projects/",
            headers={"X-Telegram-ID": str(telegram_id)},
            json={
                "id": project_id,
                "title": "Override policy",
                "initiating_run_id": str(uuid.uuid4()),
                "status": "active",
                "config": {"agent_type": "factory"},
            },
        )
    ).status_code == HTTPStatus.CREATED

    engineering = await async_client.post(
        "/api/work-admission/paid-runs",
        json={
            "id": f"override-eng-{uuid.uuid4().hex}",
            "type": "engineering",
            "project_id": project_id,
        },
    )
    qa = await async_client.post(
        "/api/work-admission/paid-runs",
        json={"id": f"override-qa-{uuid.uuid4().hex}", "type": "qa", "project_id": project_id},
    )
    assert engineering.status_code == HTTPStatus.OK, engineering.text
    assert qa.status_code == HTTPStatus.OK, qa.text
    assert engineering.json()["executor_decision"] == {
        "attempt_kind": "engineering",
        "agent_type": "claude",
        "source": "global_override",
        "policy_version": "v2",
        "reason": "Global break-glass override selected engineering executor.",
    }
    assert qa.json()["executor_decision"]["source"] == "global_override"
    assert qa.json()["executor_decision"]["agent_type"] == "codex"

    reset = {**controls, "engineering_executor_override": "none", "qa_executor_override": "none"}
    assert (
        await async_client.put("/api/work-admission/controls", json=reset)
    ).status_code == HTTPStatus.OK
    legacy = await async_client.post(
        "/api/work-admission/paid-runs",
        json={
            "id": f"legacy-eng-{uuid.uuid4().hex}",
            "type": "engineering",
            "project_id": project_id,
        },
    )
    assert legacy.status_code == HTTPStatus.OK, legacy.text
    assert legacy.json()["executor_decision"]["agent_type"] == "factory"
    assert legacy.json()["executor_decision"]["source"] == "project_pin"
