"""Service coverage for capability-backed temporary QA access records."""

import asyncio
from datetime import UTC, datetime
import uuid

from fastapi import status
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.models import TemporaryAccessGrant, WorkAdmissionAudit

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


async def _operator(async_client, *, is_admin: bool) -> tuple[int, dict[str, str]]:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    response = await async_client.post(
        "/api/users/",
        json={
            "telegram_id": telegram_id,
            "username": f"operator_{telegram_id}",
            "is_admin": is_admin,
        },
    )
    assert response.status_code == status.HTTP_201_CREATED
    return response.json()["id"], {"X-Telegram-ID": str(telegram_id)}


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
            "story_id": "story-1",
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
async def test_live_legacy_with_a_known_target_blocks_only_that_exact_target(
    async_client, db_engine
) -> None:
    project_a_id, run_a_id = await _project_with_qa_run(async_client)
    project_b_id, run_b_id = await _project_with_qa_run(async_client)
    legacy_id = f"legacy-{uuid.uuid4().hex[:8]}"
    sessions = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as session:
        session.add(
            TemporaryAccessGrant(
                id=legacy_id,
                project_id=project_a_id,
                legacy_env_key="retired-slot",
                legacy_subject="8202532144",
                target_application_id=41,
                head_sha=HEAD_SHA,
                qa_run_id=run_a_id,
                grant_run_id="legacy-grant-run",
                qa_message=_payload(project_a_id, run_a_id)["qa_message"],
                status="granted",
                granted_at=datetime.now(UTC),
            )
        )
        await session.commit()

    unrelated = await async_client.post(
        "/api/temporary-access-grants/", json=_payload(project_b_id, run_b_id)
    )
    matching = await async_client.post(
        "/api/temporary-access-grants/",
        json=_payload(project_a_id, run_a_id, target_application_id=41),
    )

    assert unrelated.status_code == status.HTTP_201_CREATED
    assert matching.status_code == status.HTTP_409_CONFLICT
    assert legacy_id in matching.json()["detail"]


@pytest.mark.asyncio
async def test_wholly_targetless_legacy_row_blocks_no_capability_target(
    async_client, db_engine
) -> None:
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
                status="revoking",
                granted_at=datetime.now(UTC),
            )
        )
        await session.commit()

    listed = await async_client.get("/api/temporary-access-grants/", params={"live": "true"})
    created = await async_client.post(
        "/api/temporary-access-grants/", json=_payload(project_id, run_id)
    )
    legacy = await async_client.get(f"/api/temporary-access-grants/{legacy_id}")

    assert listed.status_code == status.HTTP_200_OK
    assert legacy_id not in [grant["id"] for grant in listed.json()]
    assert created.status_code == status.HTTP_201_CREATED
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


@pytest.mark.asyncio
async def test_legacy_id_collision_is_refused_and_its_history_stays_visible_to_recovery(
    async_client, db_engine
) -> None:
    project_id, run_id = await _project_with_qa_run(async_client)
    legacy_id = f"tempaccess-{run_id}"
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
                status="revoked",
                granted_at=datetime.now(UTC),
                revoked_at=datetime.now(UTC),
            )
        )
        await session.commit()

    collision = await async_client.post(
        "/api/temporary-access-grants/", json=_payload(project_id, run_id, id=legacy_id)
    )
    recovery_history = await async_client.get(
        "/api/temporary-access-grants/", params={"qa_run_id": run_id}
    )

    assert collision.status_code == status.HTTP_409_CONFLICT
    assert "prior release drain" in collision.json()["detail"]
    assert recovery_history.status_code == status.HTTP_200_OK
    assert [grant["id"] for grant in recovery_history.json()] == [legacy_id]


@pytest.mark.asyncio
async def test_admin_drains_a_live_legacy_row_with_one_durable_audit(
    async_client, db_engine
) -> None:
    project_id, run_id = await _project_with_qa_run(async_client)
    admin_id, headers = await _operator(async_client, is_admin=True)
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
                status="revoking",
                granted_at=datetime.now(UTC),
            )
        )
        await session.commit()

    drained = await async_client.post(
        f"/api/temporary-access-grants/{legacy_id}/drain",
        json={"reason": "operator_drain"},
        headers=headers,
    )

    assert drained.status_code == status.HTTP_200_OK
    assert drained.json()["status"] == "revoked"
    assert drained.json()["revoke_reason"] == "operator_drain"
    assert drained.json()["revoked_at"] is not None
    async with sessions() as session:
        audit = await session.scalar(
            select(WorkAdmissionAudit).where(
                WorkAdmissionAudit.subject == "temporary_access_drain",
                WorkAdmissionAudit.reference_id == legacy_id,
            )
        )
        assert audit is not None
        assert audit.user_id == admin_id
        assert audit.actor == f"admin:{admin_id}"
        assert audit.command_payload == {"reason": "operator_drain"}
        assert audit.before_value == {"status": "revoking"}
        assert audit.after_value == {"status": "revoked"}


@pytest.mark.asyncio
async def test_admin_drains_an_escalated_target_backed_revoke(async_client, db_engine) -> None:
    project_id, run_id = await _project_with_qa_run(async_client)
    _, headers = await _operator(async_client, is_admin=True)
    created = await async_client.post(
        "/api/temporary-access-grants/", json=_payload(project_id, run_id)
    )
    assert created.status_code == status.HTTP_201_CREATED
    grant_id = created.json()["id"]
    sessions = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as session:
        grant = await session.get(TemporaryAccessGrant, grant_id)
        assert grant is not None
        grant.status = "revoke_failed"
        grant.revoke_reason = "run_terminal"
        grant.revoke_run_id = "temporary-access-revoke-exhausted"
        grant.revoke_attempts = 3
        grant.escalated_at = datetime.now(UTC)
        grant.last_error = "revoke proof failed"
        await session.commit()

    drained = await async_client.post(
        f"/api/temporary-access-grants/{grant_id}/drain",
        json={"reason": "operator_drain"},
        headers=headers,
    )

    assert drained.status_code == status.HTTP_200_OK
    assert drained.json()["status"] == "revoked"
    assert drained.json()["revoke_reason"] == "operator_drain"


@pytest.mark.asyncio
@pytest.mark.parametrize("grant_status", ["granting", "granted", "revoking"])
async def test_drain_refuses_an_unescalated_current_grant(
    async_client, db_engine, grant_status
) -> None:
    project_id, run_id = await _project_with_qa_run(async_client)
    _, headers = await _operator(async_client, is_admin=True)
    created = await async_client.post(
        "/api/temporary-access-grants/", json=_payload(project_id, run_id)
    )
    assert created.status_code == status.HTTP_201_CREATED
    grant_id = created.json()["id"]
    sessions = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as session:
        grant = await session.get(TemporaryAccessGrant, grant_id)
        assert grant is not None
        grant.status = grant_status
        await session.commit()

    refused = await async_client.post(
        f"/api/temporary-access-grants/{grant_id}/drain",
        json={"reason": "operator_drain"},
        headers=headers,
    )

    assert refused.status_code == status.HTTP_409_CONFLICT


@pytest.mark.asyncio
async def test_drain_refuses_malformed_or_unescalated_failed_target_state(
    async_client, db_engine
) -> None:
    project_id, run_id = await _project_with_qa_run(async_client)
    _, headers = await _operator(async_client, is_admin=True)
    created = await async_client.post(
        "/api/temporary-access-grants/", json=_payload(project_id, run_id)
    )
    assert created.status_code == status.HTTP_201_CREATED
    grant_id = created.json()["id"]
    sessions = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as session:
        grant = await session.get(TemporaryAccessGrant, grant_id)
        assert grant is not None
        grant.status = "revoke_failed"
        grant.escalated_at = None
        grant.revoke_run_id = None
        await session.commit()

    refused = await async_client.post(
        f"/api/temporary-access-grants/{grant_id}/drain",
        json={"reason": "operator_drain"},
        headers=headers,
    )

    assert refused.status_code == status.HTTP_409_CONFLICT

    async with sessions() as session:
        grant = await session.get(TemporaryAccessGrant, grant_id)
        assert grant is not None
        grant.escalated_at = datetime.now(UTC)
        grant.revoke_run_id = "temporary-access-revoke-malformed"
        grant.revoke_attempts = 3
        grant.revoke_reason = "operator_drain"
        grant.last_error = "revoke proof failed"
        await session.commit()

    mismatched = await async_client.post(
        f"/api/temporary-access-grants/{grant_id}/drain",
        json={"reason": "operator_drain"},
        headers=headers,
    )

    assert mismatched.status_code == status.HTTP_409_CONFLICT


@pytest.mark.asyncio
async def test_drain_requires_an_administrator(async_client, db_engine) -> None:
    project_id, run_id = await _project_with_qa_run(async_client)
    _, headers = await _operator(async_client, is_admin=False)
    legacy_id = f"legacy-{uuid.uuid4().hex[:8]}"
    sessions = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as session:
        session.add(
            TemporaryAccessGrant(
                id=legacy_id,
                project_id=project_id,
                legacy_env_key="retired-slot",
                head_sha=HEAD_SHA,
                qa_run_id=run_id,
                grant_run_id="legacy-grant-run",
                qa_message=_payload(project_id, run_id)["qa_message"],
                status="granted",
                granted_at=datetime.now(UTC),
            )
        )
        await session.commit()

    refused = await async_client.post(
        f"/api/temporary-access-grants/{legacy_id}/drain",
        json={"reason": "operator_drain"},
        headers=headers,
    )

    assert refused.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
async def test_concurrent_and_repeated_drain_calls_converge_on_one_audit(
    async_client, db_engine
) -> None:
    project_id, run_id = await _project_with_qa_run(async_client)
    _, headers = await _operator(async_client, is_admin=True)
    legacy_id = f"legacy-{uuid.uuid4().hex[:8]}"
    sessions = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as session:
        session.add(
            TemporaryAccessGrant(
                id=legacy_id,
                project_id=project_id,
                legacy_env_key="retired-slot",
                head_sha=HEAD_SHA,
                qa_run_id=run_id,
                grant_run_id="legacy-grant-run",
                qa_message=_payload(project_id, run_id)["qa_message"],
                status="granted",
                granted_at=datetime.now(UTC),
            )
        )
        await session.commit()

    url = f"/api/temporary-access-grants/{legacy_id}/drain"
    calls = await asyncio.gather(
        *[
            async_client.post(url, json={"reason": "operator_drain"}, headers=headers)
            for _ in range(2)
        ]
    )
    repeated = await async_client.post(url, json={"reason": "operator_drain"}, headers=headers)

    assert [response.status_code for response in [*calls, repeated]] == [200, 200, 200]
    assert all(response.json()["status"] == "revoked" for response in [*calls, repeated])
    async with sessions() as session:
        audit_count = await session.scalar(
            select(func.count(WorkAdmissionAudit.id)).where(
                WorkAdmissionAudit.subject == "temporary_access_drain",
                WorkAdmissionAudit.reference_id == legacy_id,
            )
        )
        assert audit_count == 1


@pytest.mark.asyncio
async def test_drain_commit_failure_rolls_back_status_and_audit(
    async_client, db_engine, monkeypatch
) -> None:
    project_id, run_id = await _project_with_qa_run(async_client)
    _, headers = await _operator(async_client, is_admin=True)
    legacy_id = f"legacy-{uuid.uuid4().hex[:8]}"
    sessions = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as session:
        session.add(
            TemporaryAccessGrant(
                id=legacy_id,
                project_id=project_id,
                legacy_env_key="retired-slot",
                head_sha=HEAD_SHA,
                qa_run_id=run_id,
                grant_run_id="legacy-grant-run",
                qa_message=_payload(project_id, run_id)["qa_message"],
                status="granted",
                granted_at=datetime.now(UTC),
            )
        )
        await session.commit()

    async def fail_commit(session) -> None:
        await session.flush()
        raise RuntimeError("forced drain commit failure")

    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="forced drain commit failure"):
        await async_client.post(
            f"/api/temporary-access-grants/{legacy_id}/drain",
            json={"reason": "operator_drain"},
            headers=headers,
        )
    monkeypatch.undo()

    async with sessions() as session:
        grant = await session.get(TemporaryAccessGrant, legacy_id)
        audit_count = await session.scalar(
            select(func.count(WorkAdmissionAudit.id)).where(
                WorkAdmissionAudit.subject == "temporary_access_drain",
                WorkAdmissionAudit.reference_id == legacy_id,
            )
        )
        assert grant is not None
        assert grant.status == "granted"
        assert grant.revoked_at is None
        assert audit_count == 0
