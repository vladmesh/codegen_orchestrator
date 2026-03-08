"""Unit tests for Run ↔ Task linkage."""

import uuid

from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import Session

from shared.models.run import Run
from shared.models.task import Task

TEST_PROJECT_UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _setup_db():
    engine = create_engine("sqlite:///:memory:")
    Task.__table__.create(engine)
    Run.__table__.create(engine)
    return engine


def test_run_without_task():
    """Existing runs without task_id still work (backward compat)."""
    engine = _setup_db()

    with Session(engine) as session:
        session.execute(
            insert(Run).values(
                id="run-1",
                type="engineering",
                status="queued",
                run_metadata={},
            )
        )
        session.commit()

    with Session(engine) as session:
        run = session.execute(select(Run).where(Run.id == "run-1")).scalar_one()
        assert run.task_id is None
        assert run.iteration is None


def test_run_linked_to_task():
    engine = _setup_db()

    with Session(engine) as session:
        session.execute(
            insert(Task).values(
                id="task-abc",
                project_id=TEST_PROJECT_UUID,
                title="Feature X",
                type="feature",
                status="in_dev",
                priority=0,
                current_iteration=1,
                max_iterations=3,
                created_by="po",
            )
        )
        session.execute(
            insert(Run).values(
                id="eng-111",
                type="engineering",
                status="running",
                task_id="task-abc",
                iteration=1,
                run_metadata={},
            )
        )
        session.commit()

    with Session(engine) as session:
        run = session.execute(select(Run).where(Run.id == "eng-111")).scalar_one()
        assert run.task_id == "task-abc"
        assert run.iteration == 1
