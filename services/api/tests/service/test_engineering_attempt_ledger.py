"""Service coverage for canonical engineering-attempt accounting."""

from http import HTTPStatus
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError


async def _user(client: AsyncClient, telegram_id: int) -> dict:
    response = await client.post(
        "/api/users/",
        json={"telegram_id": telegram_id, "username": f"ledger_{telegram_id}"},
    )
    assert response.status_code == HTTPStatus.CREATED
    return response.json()


async def _project(client: AsyncClient, telegram_id: int) -> dict:
    response = await client.post(
        "/api/projects/",
        json={
            "id": str(uuid.uuid4()),
            "title": "Ledger ownership",
            "initiating_run_id": f"init-{uuid.uuid4().hex}",
            "config": {},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert response.status_code == HTTPStatus.CREATED, response.text
    return response.json()


@pytest.mark.asyncio
async def test_project_bound_run_uses_project_owner_not_supplied_user(async_client: AsyncClient):
    owner = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    other = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    project = await _project(async_client, owner["telegram_id"])

    response = await async_client.post(
        "/api/runs/",
        json={
            "id": f"eng-{uuid.uuid4().hex}",
            "type": "engineering",
            "project_id": project["id"],
            "user_id": other["id"],
        },
    )

    assert response.status_code == HTTPStatus.CREATED
    assert response.json()["user_id"] == owner["id"]

    rewrite = await async_client.patch(
        f"/api/runs/{response.json()['id']}", json={"user_id": other["id"]}
    )
    assert rewrite.status_code == HTTPStatus.CONFLICT
    persisted = await async_client.get(f"/api/runs/{response.json()['id']}")
    assert persisted.json()["user_id"] == owner["id"]


@pytest.mark.asyncio
async def test_terminal_engineering_run_writes_one_unknown_cost_ledger_row(
    async_client: AsyncClient,
):
    owner = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    project = await _project(async_client, owner["telegram_id"])
    run_id = f"eng-{uuid.uuid4().hex}"
    created = await async_client.post(
        "/api/runs/",
        json={"id": run_id, "type": "engineering", "project_id": project["id"]},
    )
    assert created.status_code == HTTPStatus.CREATED
    terminal = {
        "status": "failed",
        "error_message": "timed out",
        "engineering_attempt": {
            "provider": "anthropic",
            "model": "claude-sonnet",
            "input_tokens": 10,
            "output_tokens": 5,
            "cost_source": "unknown",
        },
    }
    first = await async_client.patch(f"/api/runs/{run_id}", json=terminal)
    second = await async_client.patch(f"/api/runs/{run_id}", json=terminal)
    assert first.status_code == HTTPStatus.OK
    assert second.status_code == HTTPStatus.OK

    rows = await async_client.get("/api/runs/engineering-attempts", params={"run_id": run_id})
    assert rows.status_code == HTTPStatus.OK
    assert len(rows.json()) == 1
    row = rows.json()[0]
    assert row["idempotency_key"] == f"engineering-run:{run_id}"
    assert row["user_id"] == owner["id"]
    assert row["total_tokens"] == 15
    assert row["cost_source"] == "unknown"
    assert row["cost_microusd"] is None

    changed_retry = {
        **terminal,
        "engineering_attempt": {
            "provider": "other-provider",
            "model": "other-model",
            "total_tokens": 999,
            "cost_source": "unknown",
        },
    }
    assert (
        await async_client.patch(f"/api/runs/{run_id}", json=changed_retry)
    ).status_code == HTTPStatus.OK
    rows_after_retry = await async_client.get(
        "/api/runs/engineering-attempts", params={"run_id": run_id}
    )
    assert rows_after_retry.json() == rows.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled"])
async def test_terminal_engineering_run_preserves_provider_reported_cost(
    async_client: AsyncClient, terminal_status: str
):
    owner = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    project = await _project(async_client, owner["telegram_id"])
    run_id = f"eng-provider-{uuid.uuid4().hex}"
    await async_client.post(
        "/api/runs/",
        json={"id": run_id, "type": "engineering", "project_id": project["id"]},
    )
    terminal = {
        "status": terminal_status,
        "engineering_attempt": {
            "claude_evidence": {
                "provider": "anthropic",
                "model": "claude-sonnet",
                "input_tokens": 12,
                "output_tokens": 5,
                "total_tokens": 17,
                "cache_read_tokens": 4,
                "cache_write_tokens": 3,
                "cost_microusd": 40_001,
            }
        },
    }
    assert (
        await async_client.patch(f"/api/runs/{run_id}", json=terminal)
    ).status_code == HTTPStatus.OK
    assert (
        await async_client.patch(f"/api/runs/{run_id}", json=terminal)
    ).status_code == HTTPStatus.OK

    rows = await async_client.get("/api/runs/engineering-attempts", params={"run_id": run_id})
    assert rows.status_code == HTTPStatus.OK
    assert rows.json()[0]["cost_microusd"] == 40_001
    assert rows.json()[0]["cost_source"] == "provider_reported"
    assert rows.json()[0]["cache_read_tokens"] == 4
    assert rows.json()[0]["cache_write_tokens"] == 3
    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["total_tokens"] == 17
    assert run.json()["cost_usd"] == pytest.approx(0.040001)


@pytest.mark.asyncio
async def test_ledger_filters_and_owner_authorization(async_client: AsyncClient):
    owner = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    intruder = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    project = await _project(async_client, owner["telegram_id"])
    run_id = f"eng-{uuid.uuid4().hex}"
    await async_client.post(
        "/api/runs/",
        json={"id": run_id, "type": "engineering", "project_id": project["id"]},
    )
    await async_client.patch(f"/api/runs/{run_id}", json={"status": "cancelled"})

    own = await async_client.get(
        "/api/runs/engineering-attempts",
        params={"project_id": project["id"], "run_id": run_id},
        headers={"X-Telegram-ID": str(owner["telegram_id"])},
    )
    assert own.status_code == HTTPStatus.OK
    assert [row["run_id"] for row in own.json()] == [run_id]
    forbidden = await async_client.get(
        "/api/runs/engineering-attempts",
        params={"run_id": run_id},
        headers={"X-Telegram-ID": str(intruder["telegram_id"])},
    )
    assert forbidden.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.asyncio
async def test_ledger_read_is_bounded(async_client: AsyncClient):
    owner = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    project = await _project(async_client, owner["telegram_id"])
    for _ in range(2):
        run_id = f"eng-page-{uuid.uuid4().hex}"
        await async_client.post(
            "/api/runs/",
            json={"id": run_id, "type": "engineering", "project_id": project["id"]},
        )
        assert (
            await async_client.patch(f"/api/runs/{run_id}", json={"status": "cancelled"})
        ).status_code == HTTPStatus.OK

    page = await async_client.get(
        "/api/runs/engineering-attempts",
        params={"project_id": project["id"], "limit": 1},
    )
    assert page.status_code == HTTPStatus.OK
    assert len(page.json()) == 1


@pytest.mark.asyncio
async def test_project_deletion_detaches_but_retains_engineering_ledger(
    async_client: AsyncClient, db_session
):
    """Hard project deletion retains consumed-resource history without live links."""
    owner = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    project = await _project(async_client, owner["telegram_id"])
    run_id = f"eng-delete-{uuid.uuid4().hex}"
    await async_client.post(
        "/api/runs/",
        json={"id": run_id, "type": "engineering", "project_id": project["id"]},
    )
    await async_client.patch(
        f"/api/runs/{run_id}",
        json={
            "status": "completed",
            "engineering_attempt": {
                "provider": "anthropic",
                "model": "claude-sonnet",
                "input_tokens": 12,
                "output_tokens": 5,
                "cost_microusd": 40_001,
                "cost_source": "provider_reported",
            },
        },
    )

    with pytest.raises(DBAPIError, match="append-only"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "UPDATE engineering_attempt_ledger SET project_id = NULL "
                    "WHERE idempotency_key = :key"
                ),
                {"key": f"engineering-run:{run_id}"},
            )

    deleted = await async_client.delete(f"/api/projects/{project['id']}")

    assert deleted.status_code == HTTPStatus.NO_CONTENT, deleted.text
    row = (
        await db_session.execute(
            text(
                "SELECT idempotency_key, run_id, project_id, story_id, task_id, user_id, "
                "input_tokens, output_tokens, cost_microusd, cost_source "
                "FROM engineering_attempt_ledger WHERE idempotency_key = :key"
            ),
            {"key": f"engineering-run:{run_id}"},
        )
    ).one()
    assert row == (
        f"engineering-run:{run_id}",
        None,
        None,
        None,
        None,
        owner["id"],
        12,
        5,
        40_001,
        "provider_reported",
    )

    with pytest.raises(DBAPIError, match="append-only"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "UPDATE engineering_attempt_ledger SET provider = 'rewritten' "
                    "WHERE idempotency_key = :key"
                ),
                {"key": f"engineering-run:{run_id}"},
            )


@pytest.mark.asyncio
async def test_migration_backfills_only_terminal_runs_and_enforces_constraints(db_session):
    """Execute the revision against an isolated PostgreSQL schema with historical rows."""
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    schema = f"ledger_migration_{uuid.uuid4().hex}"
    migration_path = (
        Path(__file__).parents[2]
        / "migrations/versions/8d2c5e6f7a8b_add_engineering_attempt_ledger.py"
    )
    spec = importlib.util.spec_from_file_location("ledger_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run_migration(session):
        connection = session.connection()
        quoted_schema = f'"{schema}"'
        connection.execute(text(f"CREATE SCHEMA {quoted_schema}"))
        connection.execute(text(f"SET LOCAL search_path TO {quoted_schema}, public"))
        for sql in (
            "CREATE TABLE users (id integer PRIMARY KEY)",
            "CREATE TABLE projects (id uuid PRIMARY KEY, owner_id integer)",
            "CREATE TABLE stories (id varchar(255) PRIMARY KEY)",
            "CREATE TABLE tasks (id varchar(255) PRIMARY KEY)",
            """CREATE TABLE runs (
                id varchar(255) PRIMARY KEY, type varchar(50) NOT NULL,
                status varchar(50) NOT NULL, project_id uuid, story_id varchar(255),
                task_id varchar(255), created_at timestamptz NOT NULL,
                started_at timestamptz, completed_at timestamptz, agent_profile jsonb,
                input_tokens integer, output_tokens integer, total_tokens integer
            )""",
            "INSERT INTO users VALUES (1)",
            "INSERT INTO projects VALUES ('00000000-0000-0000-0000-000000000001', 1)",
            """INSERT INTO runs (id, type, status, project_id, created_at) VALUES
                ('terminal-completed', 'engineering', 'completed',
                 '00000000-0000-0000-0000-000000000001', now()),
                ('terminal-failed', 'engineering', 'failed',
                 '00000000-0000-0000-0000-000000000001', now()),
                ('terminal-cancelled', 'engineering', 'cancelled',
                 '00000000-0000-0000-0000-000000000001', now()),
                ('queued', 'engineering', 'queued',
                 '00000000-0000-0000-0000-000000000001', now()),
                ('running', 'engineering', 'running',
                 '00000000-0000-0000-0000-000000000001', now()),
                ('other', 'deploy', 'completed',
                 '00000000-0000-0000-0000-000000000001', now())""",
        ):
            connection.execute(text(sql))
        original_op = migration.op
        migration.op = Operations(MigrationContext.configure(connection))
        try:
            migration.upgrade()
            rows = (
                connection.execute(
                    text("SELECT run_id FROM engineering_attempt_ledger ORDER BY run_id")
                )
                .scalars()
                .all()
            )
            assert rows == ["terminal-cancelled", "terminal-completed", "terminal-failed"]
            for sql in (
                """INSERT INTO engineering_attempt_ledger
                   (id, idempotency_key, run_id, owner_attribution, role, occurred_at, cost_source)
                   VALUES (uuid_generate_v4(), 'engineering-run:terminal-completed-duplicate',
                   'terminal-completed', 'unknown', 'engineering', now(), 'unknown')""",
                """INSERT INTO engineering_attempt_ledger
                   (id, idempotency_key, run_id, owner_attribution, role, occurred_at,
                    cost_microusd, cost_source)
                   VALUES (uuid_generate_v4(), 'engineering-run:queued', 'queued', 'unknown',
                   'engineering', now(), 1, 'unknown')""",
                """INSERT INTO engineering_attempt_ledger
                   (id, idempotency_key, run_id, owner_attribution, role, occurred_at,
                    cost_microusd, cost_source)
                   VALUES (uuid_generate_v4(), 'engineering-run:running', 'running', 'unknown',
                   'engineering', now(), 1, 'provider_reported')""",
            ):
                try:
                    with connection.begin_nested():
                        connection.execute(text(sql))
                except IntegrityError:
                    continue
                raise AssertionError(f"database accepted invalid ledger row: {sql}")

            for sql in (
                "UPDATE engineering_attempt_ledger SET model = 'rewritten' "
                "WHERE run_id = 'terminal-completed'",
                "DELETE FROM engineering_attempt_ledger WHERE run_id = 'terminal-completed'",
            ):
                with pytest.raises(DBAPIError, match="append-only"):
                    with connection.begin_nested():
                        connection.execute(text(sql))
        finally:
            migration.op = original_op
            connection.execute(text(f"DROP SCHEMA {quoted_schema} CASCADE"))

    await db_session.run_sync(run_migration)
