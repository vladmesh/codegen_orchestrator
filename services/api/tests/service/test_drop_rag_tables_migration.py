"""The migration that removes RAG drops its four tables, and is safe to replay."""

import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text

MIGRATION_PATH = Path(__file__).parents[2] / "migrations/versions/d3f5a7c9e1b4_drop_rag_tables.py"
RAG_TABLES = ("rag_documents", "rag_chunks", "rag_conversation_summaries", "rag_messages")


def _load_migration():
    spec = importlib.util.spec_from_file_location("drop_rag_tables_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.mark.asyncio
async def test_upgrade_drops_every_rag_table_and_replays_cleanly(db_session) -> None:
    migration = _load_migration()

    def run_migration(session):
        connection = session.connection()
        schema_name = f"drop_rag_{uuid.uuid4().hex}"
        schema = f'"{schema_name}"'
        connection.execute(text(f"CREATE SCHEMA {schema}"))
        connection.execute(text(f"SET LOCAL search_path TO {schema}, public"))
        # The shapes that matter to a drop: a foreign key between two of the
        # tables and an index on each, as the original revisions left them.
        connection.execute(text("CREATE TABLE rag_documents (id integer PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE rag_chunks (id integer PRIMARY KEY, "
                "document_id integer REFERENCES rag_documents (id))"
            )
        )
        connection.execute(text("CREATE TABLE rag_conversation_summaries (id integer PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE rag_messages (id integer PRIMARY KEY)"))
        connection.execute(
            text("CREATE INDEX ix_rag_chunks_document_id ON rag_chunks (document_id)")
        )

        def present() -> set[str]:
            return {
                table
                for table in (*RAG_TABLES, "ix_rag_chunks_document_id")
                if connection.execute(text(f"SELECT to_regclass('{schema_name}.{table}')")).scalar()
            }

        original_op = migration.op
        migration.op = Operations(MigrationContext.configure(connection))
        try:
            assert present() == {*RAG_TABLES, "ix_rag_chunks_document_id"}
            migration.upgrade()
            assert present() == set()
            # IF EXISTS: a database that never had the tables, or a replay, is fine.
            migration.upgrade()
            assert present() == set()
        finally:
            migration.op = original_op
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))

    await db_session.run_sync(run_migration)


def test_downgrade_is_declared_irreversible() -> None:
    migration = _load_migration()

    with pytest.raises(NotImplementedError, match="pg_dump"):
        migration.downgrade()
