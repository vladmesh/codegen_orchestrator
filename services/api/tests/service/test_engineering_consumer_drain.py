"""Service proof for the engineering consumer's audited drain control."""

from http import HTTPStatus

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import SystemConfig, WorkAdmissionAudit


async def test_engineering_consumer_drain_is_durable_and_uses_the_proxy_actor(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = {"X-Admin-Console-Operator": "release-operator"}

    initial = await async_client.get("/api/engineering-consumer/drain")
    assert initial.status_code == HTTPStatus.OK, initial.text
    assert initial.json() == {"draining": False, "requested_at": None, "actor": None}

    bypass = await async_client.post(
        "/api/system-configs/",
        json={
            "key": "engineering.consumer_drain",
            "value": {"draining": True},
            "category": "engineering",
        },
    )
    assert bypass.status_code == HTTPStatus.FORBIDDEN

    drained = await async_client.post("/api/engineering-consumer/drain", headers=headers)
    assert drained.status_code == HTTPStatus.OK, drained.text
    assert drained.json()["draining"] is True
    assert drained.json()["actor"] == "admin_console:release-operator"
    assert drained.json()["requested_at"] is not None

    state = await db_session.get(SystemConfig, "engineering.consumer_drain")
    assert state is not None
    assert state.value["draining"] is True
    assert state.updated_by == "admin_console:release-operator"

    audit = await db_session.scalar(
        select(WorkAdmissionAudit).where(
            WorkAdmissionAudit.subject == "engineering_consumer_drain",
            WorkAdmissionAudit.actor == "admin_console:release-operator",
        )
    )
    assert audit is not None
    assert audit.outcome == "draining"
    assert audit.before_value == {"draining": False}
    assert audit.after_value["draining"] is True

    resumed = await async_client.delete("/api/engineering-consumer/drain", headers=headers)
    assert resumed.status_code == HTTPStatus.OK, resumed.text
    assert resumed.json() == {"draining": False, "requested_at": None, "actor": None}

    cleared = await db_session.get(SystemConfig, "engineering.consumer_drain")
    assert cleared is not None
    assert cleared.value == {"draining": False}
    assert cleared.updated_by == "admin_console:release-operator"
