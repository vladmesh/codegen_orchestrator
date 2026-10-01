"""Exercise schema repair against historical PostgreSQL rows, including refusal."""

from contextlib import contextmanager
from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "migrations/versions/b2d4f6a8c0e1_reconcile_schema_and_run_contracts.py"
)
OBSERVATION_COLUMNS = {
    "observed_at",
    "observation_id",
    "slot_clear_since",
    "slot_clear_readings",
    "reopened_at",
}
TIMESTAMP_TABLES = ("product_briefs", "requirement_coverages")
CREATED = datetime(2026, 8, 31, 11, 0, tzinfo=UTC)
UPDATED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@contextmanager
def _prior_schema(connection):
    spec = importlib.util.spec_from_file_location("schema_contract_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.op = Operations(MigrationContext.configure(connection))
    transaction = connection.begin_nested()
    try:
        schema = f"schema_contract_{uuid.uuid4().hex}"
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        # Only the columns and constraints this revision changes. The real
        # migration-chain schema is checked separately by test_schema_metadata.
        connection.execute(
            text(
                "CREATE TABLE temporary_access_grants ("
                "id varchar(255) PRIMARY KEY, target_application_id integer NOT NULL, "
                "target_base_url varchar(2048) NOT NULL, observed_at timestamptz, "
                "observation_id varchar(255), slot_clear_since timestamptz, "
                "slot_clear_readings integer NOT NULL DEFAULT 0, reopened_at timestamptz)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO temporary_access_grants (id, target_application_id, target_base_url) "
                "VALUES ('grant', 41, 'https://target.example.com')"
            )
        )
        for table in TIMESTAMP_TABLES:
            connection.execute(
                text(
                    f"CREATE TABLE {table} (id integer PRIMARY KEY, "
                    "created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now())"
                )
            )
            connection.execute(
                text(
                    f"INSERT INTO {table} (id, created_at, updated_at) "
                    "VALUES (:id, :created, :updated)"
                ),
                [
                    {"id": 1, "created": CREATED, "updated": UPDATED},
                    {"id": 2, "created": CREATED, "updated": None},
                    {"id": 3, "created": None, "updated": UPDATED},
                    {"id": 4, "created": None, "updated": None},
                ],
            )
        connection.execute(
            text(
                "CREATE TABLE runs (id varchar(255) PRIMARY KEY, "
                "type varchar(50) NOT NULL, status varchar(50) NOT NULL)"
            )
        )
        connection.execute(text("INSERT INTO runs VALUES ('run', 'engineering', 'queued')"))
        yield migration
    finally:
        # Roll back test DDL, data and SET LOCAL, including on assertion failure.
        transaction.rollback()


def _columns(connection, table):
    return {column["name"]: column for column in inspect(connection).get_columns(table)}


def _timestamps(connection, table):
    return connection.execute(
        text(f"SELECT id, created_at, updated_at FROM {table} ORDER BY id")
    ).all()


def _assert_prior_schema_unchanged(connection):
    assert OBSERVATION_COLUMNS <= _columns(connection, "temporary_access_grants").keys()
    for table in TIMESTAMP_TABLES:
        assert _columns(connection, table)["created_at"]["nullable"]
        assert _columns(connection, table)["updated_at"]["nullable"]
        assert _timestamps(connection, table) == [
            (1, CREATED, UPDATED),
            (2, CREATED, None),
            (3, None, UPDATED),
            (4, None, None),
        ]
    assert inspect(connection).get_check_constraints("runs") == []


@pytest.mark.asyncio
async def test_upgrade_preserves_rows_and_fills_only_missing_timestamps(db_session):
    def exercise(session):
        connection = session.connection()
        with _prior_schema(connection) as migration:
            migration_time = connection.execute(text("SELECT CURRENT_TIMESTAMP")).scalar_one()
            migration.upgrade()
            assert not OBSERVATION_COLUMNS.intersection(
                _columns(connection, "temporary_access_grants")
            )
            assert connection.execute(text("SELECT * FROM temporary_access_grants")).one() == (
                "grant",
                41,
                "https://target.example.com",
            )
            assert connection.execute(text("SELECT * FROM runs")).one() == (
                "run",
                "engineering",
                "queued",
            )
            for table in TIMESTAMP_TABLES:
                assert _timestamps(connection, table) == [
                    (1, CREATED, UPDATED),
                    (2, CREATED, CREATED),
                    (3, UPDATED, UPDATED),
                    (4, migration_time, migration_time),
                ]
                for column in ("created_at", "updated_at"):
                    assert not _columns(connection, table)[column]["nullable"]
                    with pytest.raises(IntegrityError), connection.begin_nested():
                        connection.execute(text(f"UPDATE {table} SET {column} = NULL WHERE id = 1"))
                connection.execute(text(f"INSERT INTO {table} (id) VALUES (5)"))
                assert _timestamps(connection, table)[-1] == (5, migration_time, migration_time)
            for column in ("type", "status"):
                assert str(_columns(connection, "runs")[column]["type"]) == "VARCHAR(50)"
            assert {c["name"] for c in inspect(connection).get_check_constraints("runs")} == {
                "ck_runs_type_valid",
                "ck_runs_status_valid",
            }

    await db_session.run_sync(exercise)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "column,value",
    [
        ("observed_at", CREATED),
        ("observation_id", "historic-observation"),
        ("slot_clear_since", CREATED),
        ("slot_clear_readings", 1),
        ("slot_clear_readings", -1),
        ("reopened_at", UPDATED),
    ],
)
async def test_upgrade_refuses_each_populated_observation_before_any_change(
    db_session, column, value
):
    def exercise(session):
        connection = session.connection()
        with _prior_schema(connection) as migration:
            connection.execute(
                text(f"UPDATE temporary_access_grants SET {column} = :value"), {"value": value}
            )
            with pytest.raises(RuntimeError, match="retired temporary-access observations.*grant"):
                migration.upgrade()
            _assert_prior_schema_unchanged(connection)
            assert (
                connection.execute(
                    text(f"SELECT {column} FROM temporary_access_grants")
                ).scalar_one()
                == value
            )

    await db_session.run_sync(exercise)


@pytest.mark.asyncio
@pytest.mark.parametrize("column,value", [("type", "legacy-worker"), ("status", "done")])
async def test_upgrade_refuses_unknown_run_values_before_any_change(db_session, column, value):
    def exercise(session):
        connection = session.connection()
        with _prior_schema(connection) as migration:
            connection.execute(text(f"UPDATE runs SET {column} = :value"), {"value": value})
            with pytest.raises(RuntimeError, match="invalid Run type/status.*run"):
                migration.upgrade()
            _assert_prior_schema_unchanged(connection)
            assert connection.execute(text(f"SELECT {column} FROM runs")).scalar_one() == value

    await db_session.run_sync(exercise)


@pytest.mark.asyncio
async def test_downgrade_restores_schema_preserves_dates_and_can_upgrade_again(db_session):
    def exercise(session):
        connection = session.connection()
        with _prior_schema(connection) as migration:
            migration.upgrade()
            timestamps = {table: _timestamps(connection, table) for table in TIMESTAMP_TABLES}
            migration.downgrade()
            columns = _columns(connection, "temporary_access_grants")
            assert OBSERVATION_COLUMNS <= columns.keys()
            assert not columns["slot_clear_readings"]["nullable"]
            assert all(
                columns[column]["nullable"]
                for column in OBSERVATION_COLUMNS - {"slot_clear_readings"}
            )
            assert connection.execute(
                text(
                    "SELECT observed_at, observation_id, slot_clear_since, "
                    "slot_clear_readings, reopened_at FROM temporary_access_grants"
                )
            ).one() == (None, None, None, 0, None)
            for table in TIMESTAMP_TABLES:
                assert _timestamps(connection, table) == timestamps[table]
                assert _columns(connection, table)["created_at"]["nullable"]
                assert _columns(connection, table)["updated_at"]["nullable"]
            assert inspect(connection).get_check_constraints("runs") == []
            # The downgrade removes the vocabulary checks but leaves VARCHAR
            # and the data alone; upgrading again reinstates enforcement.
            connection.execute(text("UPDATE runs SET type = 'legacy-worker', status = 'done'"))
            connection.execute(text("UPDATE runs SET type = 'engineering', status = 'queued'"))
            migration.upgrade()
            for table in TIMESTAMP_TABLES:
                assert _timestamps(connection, table) == timestamps[table]
            with pytest.raises(IntegrityError, match="ck_runs_status_valid"):
                with connection.begin_nested():
                    connection.execute(text("UPDATE runs SET status = 'done'"))

    await db_session.run_sync(exercise)
