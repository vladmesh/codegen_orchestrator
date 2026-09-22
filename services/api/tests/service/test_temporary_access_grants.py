"""Service coverage for capability-backed temporary QA access records."""

import asyncio
from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import status
import pytest
from sqlalchemy import func, select, text
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
async def test_live_target_holder_blocks_only_that_exact_target(async_client) -> None:
    project_a_id, run_a_id = await _project_with_qa_run(async_client)
    project_b_id, run_b_id = await _project_with_qa_run(async_client)
    holder = await async_client.post(
        "/api/temporary-access-grants/",
        json=_payload(project_a_id, run_a_id, target_application_id=41),
    )
    assert holder.status_code == status.HTTP_201_CREATED

    unrelated = await async_client.post(
        "/api/temporary-access-grants/", json=_payload(project_b_id, run_b_id)
    )
    matching = await async_client.post(
        "/api/temporary-access-grants/",
        json=_payload(project_a_id, run_a_id, target_application_id=41),
    )

    assert unrelated.status_code == status.HTTP_201_CREATED
    assert matching.status_code == status.HTTP_409_CONFLICT
    assert holder.json()["id"] in matching.json()["detail"]


async def _escalated_revoke_failed_grant(async_client, db_engine) -> str:
    project_id, run_id = await _project_with_qa_run(async_client)
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
    return grant_id


@pytest.mark.asyncio
async def test_admin_drains_an_escalated_target_backed_revoke_with_one_durable_audit(
    async_client, db_engine
) -> None:
    admin_id, headers = await _operator(async_client, is_admin=True)
    grant_id = await _escalated_revoke_failed_grant(async_client, db_engine)

    drained = await async_client.post(
        f"/api/temporary-access-grants/{grant_id}/drain",
        json={"reason": "operator_drain"},
        headers=headers,
    )

    assert drained.status_code == status.HTTP_200_OK
    assert drained.json()["status"] == "revoked"
    assert drained.json()["revoke_reason"] == "operator_drain"
    assert drained.json()["revoked_at"] is not None
    sessions = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as session:
        audit = await session.scalar(
            select(WorkAdmissionAudit).where(
                WorkAdmissionAudit.subject == "temporary_access_drain",
                WorkAdmissionAudit.reference_id == grant_id,
            )
        )
        assert audit is not None
        assert audit.user_id == admin_id
        assert audit.actor == f"admin:{admin_id}"
        assert audit.command_payload == {"reason": "operator_drain"}
        assert audit.before_value == {"status": "revoke_failed"}
        assert audit.after_value == {"status": "revoked"}


@pytest.mark.asyncio
async def test_drain_of_an_unknown_grant_is_not_found(async_client) -> None:
    _, headers = await _operator(async_client, is_admin=True)

    missing = await async_client.post(
        f"/api/temporary-access-grants/tempaccess-missing-{uuid.uuid4().hex[:8]}/drain",
        json={"reason": "operator_drain"},
        headers=headers,
    )

    assert missing.status_code == status.HTTP_404_NOT_FOUND


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
    _, headers = await _operator(async_client, is_admin=False)
    grant_id = await _escalated_revoke_failed_grant(async_client, db_engine)

    refused = await async_client.post(
        f"/api/temporary-access-grants/{grant_id}/drain",
        json={"reason": "operator_drain"},
        headers=headers,
    )

    assert refused.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
async def test_concurrent_and_repeated_drain_calls_converge_on_one_audit(
    async_client, db_engine
) -> None:
    _, headers = await _operator(async_client, is_admin=True)
    grant_id = await _escalated_revoke_failed_grant(async_client, db_engine)
    sessions = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    url = f"/api/temporary-access-grants/{grant_id}/drain"
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
                WorkAdmissionAudit.reference_id == grant_id,
            )
        )
        assert audit_count == 1


@pytest.mark.asyncio
async def test_drain_commit_failure_rolls_back_status_and_audit(
    async_client, db_engine, monkeypatch
) -> None:
    _, headers = await _operator(async_client, is_admin=True)
    grant_id = await _escalated_revoke_failed_grant(async_client, db_engine)
    sessions = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def fail_commit(session) -> None:
        await session.flush()
        raise RuntimeError("forced drain commit failure")

    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="forced drain commit failure"):
        await async_client.post(
            f"/api/temporary-access-grants/{grant_id}/drain",
            json={"reason": "operator_drain"},
            headers=headers,
        )
    monkeypatch.undo()

    async with sessions() as session:
        grant = await session.get(TemporaryAccessGrant, grant_id)
        audit_count = await session.scalar(
            select(func.count(WorkAdmissionAudit.id)).where(
                WorkAdmissionAudit.subject == "temporary_access_drain",
                WorkAdmissionAudit.reference_id == grant_id,
            )
        )
        assert grant is not None
        assert grant.status == "revoke_failed"
        assert grant.revoked_at is None
        assert audit_count == 0


@pytest.mark.asyncio
async def test_a_skipped_capability_run_never_proves_a_grant(async_client) -> None:
    """The 2026-09-15 runs completed SUCCESS as a same-SHA skip and never reached the product."""
    project_id, run_id = await _project_with_qa_run(async_client)
    payload = _payload(project_id, run_id)
    created = await async_client.post("/api/temporary-access-grants/", json=payload)
    assert created.status_code == status.HTTP_201_CREATED
    operation = await async_client.post(
        "/api/runs/",
        json={"id": payload["grant_run_id"], "type": "deploy", "project_id": project_id},
    )
    assert operation.status_code == status.HTTP_201_CREATED, operation.text
    result = {"deploy_outcome": "success", "application_id": 42}
    skipped = await async_client.patch(
        f"/api/runs/{payload['grant_run_id']}",
        json={
            "status": "completed",
            "result": {**result, "skipped_reason": "already_deployed_same_sha"},
        },
    )
    assert skipped.status_code == status.HTTP_200_OK, skipped.text

    refused = await async_client.patch(
        f"/api/temporary-access-grants/{payload['id']}", json={"status": "granted"}
    )

    assert refused.status_code == status.HTTP_409_CONFLICT
    assert "has not proved" in refused.json()["detail"]
    stored = await async_client.get(f"/api/temporary-access-grants/{payload['id']}")
    assert stored.json()["status"] == "granting"


def _load_retirement_migration():
    migration_path = (
        Path(__file__).parents[2]
        / "migrations/versions/4d8e1f2a3b5c_retire_targetless_temporary_access.py"
    )
    spec = importlib.util.spec_from_file_location("retire_targetless_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _run_against_prior_schema(session, rows: str, check) -> None:
    """Build the pre-revision table in an isolated schema, seed it and run ``check``."""
    migration = _load_retirement_migration()
    schema = f"tempaccess_migration_{uuid.uuid4().hex}"
    connection = session.connection()
    quoted_schema = f'"{schema}"'
    connection.execute(text(f"CREATE SCHEMA {quoted_schema}"))
    connection.execute(text(f"SET LOCAL search_path TO {quoted_schema}, public"))
    for sql in (
        """CREATE TABLE temporary_access_grants (
            id varchar(255) PRIMARY KEY, project_id uuid NOT NULL,
            env_key varchar(255), subject varchar(255),
            target_application_id integer, target_base_url varchar(2048),
            status varchar(50) NOT NULL
        )""",
        """CREATE UNIQUE INDEX uq_temporary_access_grants_live_target
            ON temporary_access_grants (project_id, target_application_id)
            WHERE status != 'revoked' AND target_application_id IS NOT NULL""",
        rows,
    ):
        connection.execute(text(sql))

    def columns() -> dict[str, str]:
        return dict(
            connection.execute(
                text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = 'temporary_access_grants'"
                ),
                {"schema": schema},
            ).all()
        )

    def ids() -> list[str]:
        return (
            connection.execute(text("SELECT id FROM temporary_access_grants ORDER BY id"))
            .scalars()
            .all()
        )

    def index_definition() -> str:
        return connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = :schema "
                "AND indexname = 'uq_temporary_access_grants_live_target'"
            ),
            {"schema": schema},
        ).scalar_one()

    original_op = migration.op
    migration.op = Operations(MigrationContext.configure(connection))
    try:
        check(migration, columns, ids, index_definition)
    finally:
        migration.op = original_op
        connection.execute(text(f"DROP SCHEMA {quoted_schema} CASCADE"))


_PROJECT = "'00000000-0000-0000-0000-000000000001'"


@pytest.mark.asyncio
async def test_retirement_migration_deletes_revoked_targetless_history_and_round_trips(
    db_session,
) -> None:
    rows = f"""INSERT INTO temporary_access_grants
        (id, project_id, env_key, subject, target_application_id, target_base_url, status)
        VALUES
        ('targetless-revoked', {_PROJECT}, 'retired-slot', '8202532144', NULL, NULL, 'revoked'),
        ('half-target-revoked', {_PROJECT}, 'retired-slot', NULL, 41, NULL, 'revoked'),
        ('target-live', {_PROJECT}, NULL, NULL, 42, 'https://exact.example.com', 'granted'),
        ('target-revoked', {_PROJECT}, NULL, NULL, 42, 'https://exact.example.com', 'revoked')
    """

    def check(migration, columns, ids, index_definition) -> None:
        index_before = index_definition()

        migration.upgrade()

        assert ids() == ["target-live", "target-revoked"]
        upgraded = columns()
        assert "env_key" not in upgraded
        assert "subject" not in upgraded
        assert upgraded["target_application_id"] == "NO"
        assert upgraded["target_base_url"] == "NO"
        assert index_definition() == index_before

        migration.downgrade()

        downgraded = columns()
        assert downgraded["env_key"] == "YES"
        assert downgraded["subject"] == "YES"
        assert downgraded["target_application_id"] == "YES"
        assert downgraded["target_base_url"] == "YES"
        assert ids() == ["target-live", "target-revoked"]
        assert index_definition() == index_before

    await db_session.run_sync(lambda session: _run_against_prior_schema(session, rows, check))


@pytest.mark.asyncio
async def test_retirement_migration_refuses_live_targetless_grants_and_changes_nothing(
    db_session,
) -> None:
    rows = f"""INSERT INTO temporary_access_grants
        (id, project_id, env_key, subject, target_application_id, target_base_url, status)
        VALUES
        ('targetless-revoked', {_PROJECT}, 'retired-slot', NULL, NULL, NULL, 'revoked'),
        ('targetless-revoking', {_PROJECT}, 'retired-slot', NULL, NULL, NULL, 'revoking'),
        ('half-target-granted', {_PROJECT}, 'retired-slot', NULL, 41, NULL, 'granted'),
        ('target-live', {_PROJECT}, NULL, NULL, 42, 'https://exact.example.com', 'granted')
    """

    def check(migration, columns, ids, index_definition) -> None:
        before = columns()

        with pytest.raises(RuntimeError, match="refuses to delete live target-less") as refused:
            migration.upgrade()

        assert "half-target-granted" in str(refused.value)
        assert "targetless-revoking" in str(refused.value)
        assert "targetless-revoked" not in str(refused.value)
        assert columns() == before
        assert before["env_key"] == "YES"
        assert before["target_base_url"] == "YES"
        assert ids() == [
            "half-target-granted",
            "target-live",
            "targetless-revoked",
            "targetless-revoking",
        ]

    await db_session.run_sync(lambda session: _run_against_prior_schema(session, rows, check))
