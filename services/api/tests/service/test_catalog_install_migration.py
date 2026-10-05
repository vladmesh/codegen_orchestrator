"""Apply the real migration to historical PostgreSQL Tasks in an isolated schema."""

import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError


@pytest.mark.asyncio
async def test_upgrade_preserves_ordinary_rows_and_requires_install_ownership(db_session):
    def exercise(session):
        connection = session.connection()
        transaction = connection.begin_nested()
        try:
            schema = f"catalog_install_{uuid.uuid4().hex}"
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            connection.execute(
                text(
                    "CREATE TABLE tasks (id text PRIMARY KEY, type text NOT NULL, "
                    "story_id text, repository_id text)"
                )
            )
            connection.execute(text("INSERT INTO tasks VALUES ('ordinary', 'feature', NULL, NULL)"))
            path = Path(__file__).parents[2] / "migrations/versions/f1a3c5e7b9d2_catalog_install.py"
            spec = importlib.util.spec_from_file_location("install_migration", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.op = Operations(MigrationContext.configure(connection))
            module.upgrade()
            assert {"install", "install_operation"}.issubset(
                column["name"] for column in inspect(connection).get_columns("tasks", schema=schema)
            )
            assert connection.execute(
                text("SELECT install, install_operation FROM tasks WHERE id='ordinary'")
            ).one() == (None, None)
            for values in [("install", None, None), ("install", "story", None)]:
                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(
                        text(
                            "INSERT INTO tasks(id,type,story_id,repository_id) "
                            "VALUES ('invalid', :kind, :story, :repo)"
                        ),
                        dict(zip(("kind", "story", "repo"), values, strict=True)),
                    )
            connection.execute(
                text(
                    "INSERT INTO tasks VALUES "
                    "('owned', 'install', 'story', 'repo', '{}'::json, NULL)"
                )
            )
            module.downgrade()
            assert connection.execute(text("SELECT id FROM tasks ORDER BY id")).scalars().all() == [
                "ordinary",
                "owned",
            ]
        finally:
            transaction.rollback()

    await db_session.run_sync(exercise)
