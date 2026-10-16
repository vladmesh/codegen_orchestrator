"""The users timestamp migration keeps every stored instant in both directions."""

import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text

MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "migrations/versions/adafaa34bf3d_users_timestamps_timezone_aware.py"
)
CREATED_EPOCH = 1_785_574_800
LAST_SEEN_EPOCH = 1_787_002_383


def _load_migration():
    spec = importlib.util.spec_from_file_location("users_timestamps_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.mark.asyncio
async def test_upgrade_reads_naive_values_as_utc_and_downgrade_restores_them(db_session) -> None:
    migration = _load_migration()

    def run_migration(session):
        connection = session.connection()
        schema = f'"users_tz_{uuid.uuid4().hex}"'
        connection.execute(text(f"CREATE SCHEMA {schema}"))
        connection.execute(text(f"SET LOCAL search_path TO {schema}, public"))
        # A session zone other than UTC must not move any stored hour.
        connection.execute(text("SET LOCAL TimeZone TO 'America/New_York'"))
        connection.execute(
            text(
                "CREATE TABLE users (id integer PRIMARY KEY, "
                "created_at timestamp without time zone NOT NULL, "
                "last_seen timestamp without time zone NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO users VALUES (1, to_timestamp(:created) AT TIME ZONE 'UTC', "
                "to_timestamp(:last_seen) AT TIME ZONE 'UTC')"
            ),
            {"created": CREATED_EPOCH, "last_seen": LAST_SEEN_EPOCH},
        )

        def stored() -> tuple[str, str, float, float]:
            # EXTRACT(EPOCH) of a naive value is its nominal seconds read as UTC.
            row = connection.execute(
                text(
                    "SELECT pg_typeof(created_at)::text, pg_typeof(last_seen)::text, "
                    "EXTRACT(EPOCH FROM created_at), EXTRACT(EPOCH FROM last_seen) FROM users"
                )
            ).one()
            return row[0], row[1], float(row[2]), float(row[3])

        original_op = migration.op
        migration.op = Operations(MigrationContext.configure(connection))
        try:
            migration.upgrade()
            assert stored() == (
                "timestamp with time zone",
                "timestamp with time zone",
                CREATED_EPOCH,
                LAST_SEEN_EPOCH,
            )
            migration.downgrade()
            assert stored() == (
                "timestamp without time zone",
                "timestamp without time zone",
                CREATED_EPOCH,
                LAST_SEEN_EPOCH,
            )
        finally:
            migration.op = original_op
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))

    await db_session.run_sync(run_migration)
