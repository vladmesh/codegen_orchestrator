"""Service coverage for capability-backed temporary QA access records."""

from datetime import UTC, datetime
import uuid

from fastapi import status
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.models import TemporaryAccessGrant

HEAD_SHA = "b" * 40


async def _project_with_qa_run(async_client) -> tuple[str, str]:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = str(uuid.uuid4())
    user = await async_client.post(
        "/api/users/", json={"telegram_id": telegram_id, "username": f"qa_{telegram_id}"}
    )
    assert user.status_code == status.HTTP_201_CREATED
    project = await async_client.post(
        "/api/projects/",
        json={
            "id": project_id,
            "initiating_run_id": "test-run-1",
            "title": "Temporary access",
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
    return project_id, run_id


def _payload(project_id: str, run_id: str, **overrides) -> dict:
    payload = {
        "id": f"tempaccess-{uuid.uuid4().hex[:8]}",
        "project_id": project_id,
        "channel": "telegram",
        "external_id": "8202532144",
        "target_application_id": 42,
        "target_base_url": "https://exact.example.com",
        "head_sha": HEAD_SHA,
        "qa_run_id": run_id,
        "grant_run_id": f"temporary-access-grant-{uuid.uuid4().hex[:8]}",
        "qa_message": {
            "project_id": project_id,
            "initiating_run_id": "live-1",
            "telegram_chat_id": "",
            "deployed_url": "https://exact.example.com",
            "application_id": 42,
            "acceptance_criteria": "the bot answers /start",
            "run_id": run_id,
        },
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_create_is_idempotent_and_binds_the_exact_non_secret_target(async_client) -> None:
    project_id, run_id = await _project_with_qa_run(async_client)
    payload = _payload(project_id, run_id)

    first = await async_client.post("/api/temporary-access-grants/", json=payload)
    second = await async_client.post("/api/temporary-access-grants/", json=payload)

    assert first.status_code == status.HTTP_201_CREATED
    assert second.status_code == status.HTTP_201_CREATED
    stored = first.json()
    assert stored["target_application_id"] == 42
    assert stored["target_base_url"] == "https://exact.example.com"
    assert stored["channel"] == "telegram"
    assert stored["external_id"] == "8202532144"
    assert "capability" not in stored
    assert second.json()["granted_at"] == stored["granted_at"]


@pytest.mark.asyncio
async def test_live_target_holder_is_refused_without_creating_a_second_grant(async_client) -> None:
    project_id, run_id = await _project_with_qa_run(async_client)
    first = await async_client.post(
        "/api/temporary-access-grants/", json=_payload(project_id, run_id)
    )
    assert first.status_code == status.HTTP_201_CREATED

    conflict = await async_client.post(
        "/api/temporary-access-grants/",
        json=_payload(project_id, run_id, external_id="8202532145"),
    )

    assert conflict.status_code == status.HTTP_409_CONFLICT
    assert "held" in conflict.json()["detail"]


@pytest.mark.asyncio
async def test_live_legacy_blocks_only_new_capability_handoffs(async_client, db_engine) -> None:
    project_id, run_id = await _project_with_qa_run(async_client)
    legacy_id = f"legacy-{uuid.uuid4().hex[:8]}"
    sessions = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as session:
        session.add(
            TemporaryAccessGrant(
                id=legacy_id,
                project_id=project_id,
                legacy_env_key="retired-slot",
                legacy_subject="8202532144",
                head_sha=HEAD_SHA,
                qa_run_id=run_id,
                grant_run_id="legacy-grant-run",
                qa_message=_payload(project_id, run_id)["qa_message"],
                status="granted",
                granted_at=datetime.now(UTC),
            )
        )
        await session.commit()

    listed = await async_client.get("/api/temporary-access-grants/", params={"live": "true"})
    blocked = await async_client.post(
        "/api/temporary-access-grants/", json=_payload(project_id, run_id)
    )
    legacy = await async_client.get(f"/api/temporary-access-grants/{legacy_id}")

    assert listed.status_code == status.HTTP_200_OK
    assert legacy_id not in [grant["id"] for grant in listed.json()]
    assert blocked.status_code == status.HTTP_409_CONFLICT
    assert "prior release drain" in blocked.json()["detail"]
    assert legacy.status_code == status.HTTP_409_CONFLICT

    async with sessions() as session:
        grant = await session.get(TemporaryAccessGrant, legacy_id)
        assert grant is not None
        grant.status = "revoked"
        grant.revoked_at = datetime.now(UTC)
        await session.commit()

    history = await async_client.get(f"/api/temporary-access-grants/{legacy_id}")
    assert history.status_code == status.HTTP_200_OK
    assert history.json()["status"] == "revoked"
