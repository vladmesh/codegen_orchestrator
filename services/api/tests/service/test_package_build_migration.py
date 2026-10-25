"""Apply the package build migration to historical PostgreSQL Tasks in an isolated schema."""

import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

MIGRATION_PATH = (
    Path(__file__).parents[2] / "migrations/versions/c7e2a9f4d6b1_task_package_build.py"
)
INSERT = (
    "INSERT INTO tasks(id, type, story_id, repository_id, install, package_build) "
    "VALUES (:id, :kind, :story, :repo, CAST(:install AS json), CAST(:build AS json))"
)


@pytest.mark.asyncio
async def test_upgrade_keeps_old_rows_without_intent_and_requires_owned_feature(db_session):
    def exercise(session):
        connection = session.connection()
        transaction = connection.begin_nested()
        try:
            schema = f"package_build_{uuid.uuid4().hex}"
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            connection.execute(
                text(
                    "CREATE TABLE tasks (id text PRIMARY KEY, type text NOT NULL, "
                    "story_id text, repository_id text, install json)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO tasks VALUES ('ordinary', 'feature', 'story', 'repo', NULL), "
                    "('installing', 'install', 'story', 'repo', '{}'::json)"
                )
            )
            spec = importlib.util.spec_from_file_location("package_build_migration", MIGRATION_PATH)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.op = Operations(MigrationContext.configure(connection))
            module.upgrade()
            assert "package_build" in {
                column["name"] for column in inspect(connection).get_columns("tasks", schema=schema)
            }
            assert connection.execute(
                text("SELECT id, package_build FROM tasks ORDER BY id")
            ).all() == [("installing", None), ("ordinary", None)]
            for kind, story, repo, install in [
                ("install", "story", "repo", "{}"),
                ("fix", "story", "repo", None),
                ("feature", None, "repo", None),
                ("feature", "story", None, None),
                ("feature", "story", "repo", "{}"),
            ]:
                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(
                        text(INSERT),
                        {
                            "id": "invalid",
                            "kind": kind,
                            "story": story,
                            "repo": repo,
                            "install": install,
                            "build": "{}",
                        },
                    )
            connection.execute(
                text(INSERT),
                {
                    "id": "owned",
                    "kind": "feature",
                    "story": "story",
                    "repo": "repo",
                    "install": None,
                    "build": "{}",
                },
            )
            module.downgrade()
            assert connection.execute(text("SELECT id FROM tasks ORDER BY id")).scalars().all() == [
                "installing",
                "ordinary",
                "owned",
            ]
        finally:
            transaction.rollback()

    await db_session.run_sync(exercise)
