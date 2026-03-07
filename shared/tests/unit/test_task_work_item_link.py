"""Unit tests for Task ↔ WorkItem linkage."""

from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import Session

from shared.models.task import Task
from shared.models.work_item import WorkItem


def _setup_db():
    engine = create_engine("sqlite:///:memory:")
    WorkItem.__table__.create(engine)
    Task.__table__.create(engine)
    return engine


def test_task_without_work_item():
    """Existing tasks without work_item_id still work (backward compat)."""
    engine = _setup_db()

    with Session(engine) as session:
        session.execute(
            insert(Task).values(
                id="task-1",
                type="engineering",
                status="queued",
                task_metadata={},
            )
        )
        session.commit()

    with Session(engine) as session:
        task = session.execute(select(Task).where(Task.id == "task-1")).scalar_one()
        assert task.work_item_id is None
        assert task.iteration is None


def test_task_linked_to_work_item():
    engine = _setup_db()

    with Session(engine) as session:
        session.execute(
            insert(WorkItem).values(
                id="wi-abc",
                project_id="proj-test",
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
            insert(Task).values(
                id="eng-111",
                type="engineering",
                status="running",
                work_item_id="wi-abc",
                iteration=1,
                task_metadata={},
            )
        )
        session.commit()

    with Session(engine) as session:
        task = session.execute(select(Task).where(Task.id == "eng-111")).scalar_one()
        assert task.work_item_id == "wi-abc"
        assert task.iteration == 1
