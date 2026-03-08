"""Unit tests for Task and TaskEvent models + enums + transition matrix."""

from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import Session

from shared.contracts.dto.task import (
    VALID_TRANSITIONS,
    TaskEventType,
    TaskStatus,
    TaskType,
)
from shared.models.task import Task, TaskEvent


def _setup_db():
    """Create in-memory SQLite DB with tasks and task_events tables."""
    engine = create_engine("sqlite:///:memory:")
    Task.__table__.create(engine)
    TaskEvent.__table__.create(engine)
    return engine


# --- Enum tests ---


def test_task_status_values():
    assert TaskStatus.BACKLOG == "backlog"
    assert TaskStatus.TODO == "todo"
    assert TaskStatus.IN_DEV == "in_dev"
    assert TaskStatus.DONE == "done"
    assert TaskStatus.BLOCKED == "blocked"
    assert TaskStatus.FAILED == "failed"
    assert TaskStatus.CANCELLED == "cancelled"


def test_task_type_values():
    assert TaskType.CREATE == "create"
    assert TaskType.FEATURE == "feature"
    assert TaskType.FIX == "fix"
    assert TaskType.REFACTOR == "refactor"


def test_task_event_type_values():
    assert TaskEventType.STATUS_CHANGE == "status_change"
    assert TaskEventType.ITERATION_START == "iteration_start"
    assert TaskEventType.ITERATION_END == "iteration_end"
    assert TaskEventType.NOTE == "note"
    assert TaskEventType.COMMENT == "comment"
    assert not hasattr(TaskEventType, "STEP_START")
    assert not hasattr(TaskEventType, "STEP_DONE")


# --- Transition matrix tests ---


def test_backlog_can_go_to_todo():
    assert TaskStatus.TODO in VALID_TRANSITIONS[TaskStatus.BACKLOG]


def test_backlog_cannot_go_to_done():
    assert TaskStatus.DONE not in VALID_TRANSITIONS[TaskStatus.BACKLOG]


def test_done_can_reopen_to_backlog():
    assert TaskStatus.BACKLOG in VALID_TRANSITIONS[TaskStatus.DONE]


def test_cancelled_is_terminal():
    assert VALID_TRANSITIONS[TaskStatus.CANCELLED] == set()


def test_all_statuses_have_transitions():
    for status in TaskStatus:
        assert status in VALID_TRANSITIONS, f"{status} missing from VALID_TRANSITIONS"


def test_in_dev_can_go_to_in_ci():
    allowed = VALID_TRANSITIONS[TaskStatus.IN_DEV]
    assert TaskStatus.IN_CI in allowed
    assert TaskStatus.TESTING not in allowed  # must go through in_ci first


def test_in_ci_transitions():
    allowed = VALID_TRANSITIONS[TaskStatus.IN_CI]
    assert TaskStatus.IN_DEV in allowed  # CI red → back to dev
    assert TaskStatus.TESTING in allowed  # CI green → testing
    assert TaskStatus.DONE not in allowed  # must go through testing
    assert TaskStatus.FAILED in allowed
    assert TaskStatus.CANCELLED in allowed


def test_in_ci_status_value():
    assert TaskStatus.IN_CI == "in_ci"
    assert not hasattr(TaskStatus, "IN_REVIEW")


def test_in_dev_can_go_to_blocked():
    assert TaskStatus.BLOCKED in VALID_TRANSITIONS[TaskStatus.IN_DEV]


def test_blocked_transitions():
    allowed = VALID_TRANSITIONS[TaskStatus.BLOCKED]
    assert TaskStatus.IN_DEV in allowed  # blocker resolved
    assert TaskStatus.BACKLOG in allowed  # deprioritize
    assert TaskStatus.CANCELLED in allowed
    assert TaskStatus.DONE not in allowed  # can't skip to done


def test_testing_can_return_to_in_dev():
    assert TaskStatus.IN_DEV in VALID_TRANSITIONS[TaskStatus.TESTING]


# --- Model instantiation tests ---


def test_task_defaults():
    engine = _setup_db()

    with Session(engine) as session:
        task = Task(id="task-test1", project_id="proj-test", title="Test feature")
        session.add(task)
        session.commit()
        session.refresh(task)

        assert task.status == TaskStatus.BACKLOG.value
        assert task.type == TaskType.FEATURE.value
        assert task.priority == 0
        assert task.current_iteration == 0
        assert task.max_iterations == 3
        assert task.created_by == "system"


def test_task_persist_and_read():
    engine = _setup_db()

    with Session(engine) as session:
        session.execute(
            insert(Task).values(
                id="task-abc",
                project_id="proj-test",
                title="Add statistics button",
                description="Full description here",
                type=TaskType.FEATURE.value,
                status=TaskStatus.BACKLOG.value,
                priority=0,
                current_iteration=0,
                max_iterations=3,
                created_by="po",
            )
        )
        session.commit()

    with Session(engine) as session:
        task = session.execute(select(Task).where(Task.id == "task-abc")).scalar_one()
        assert task.title == "Add statistics button"
        assert task.description == "Full description here"
        assert task.type == TaskType.FEATURE.value
        assert task.status == TaskStatus.BACKLOG.value
        assert task.created_by == "po"


def test_task_event_persist_and_read():
    engine = _setup_db()

    with Session(engine) as session:
        session.execute(
            insert(Task).values(
                id="task-abc",
                project_id="proj-test",
                title="Test",
                type="feature",
                status="backlog",
                priority=0,
                current_iteration=0,
                max_iterations=3,
                created_by="system",
            )
        )
        session.execute(
            insert(TaskEvent).values(
                task_id="task-abc",
                event_type=TaskEventType.STATUS_CHANGE.value,
                from_status=TaskStatus.BACKLOG.value,
                to_status=TaskStatus.TODO.value,
                actor="po",
                details={},
            )
        )
        session.commit()

    with Session(engine) as session:
        event = session.execute(
            select(TaskEvent).where(TaskEvent.task_id == "task-abc")
        ).scalar_one()
        assert event.event_type == TaskEventType.STATUS_CHANGE.value
        assert event.from_status == TaskStatus.BACKLOG.value
        assert event.to_status == TaskStatus.TODO.value
        assert event.actor == "po"


def test_task_plan_field():
    engine = _setup_db()

    with Session(engine) as session:
        session.execute(
            insert(Task).values(
                id="task-plan",
                project_id="proj-test",
                title="Feature with plan",
                type="feature",
                status="in_dev",
                priority=0,
                current_iteration=0,
                max_iterations=3,
                created_by="system",
                plan="## Step 1\nDo the thing\n## Step 2\nVerify",
            )
        )
        session.commit()

    with Session(engine) as session:
        task = session.execute(select(Task).where(Task.id == "task-plan")).scalar_one()
        assert task.plan == "## Step 1\nDo the thing\n## Step 2\nVerify"


def test_task_need_e2e_defaults_to_false():
    engine = _setup_db()

    with Session(engine) as session:
        task = Task(id="task-e2e", project_id="proj-test", title="E2E test")
        session.add(task)
        session.commit()
        session.refresh(task)

        assert task.need_e2e is False


def test_task_need_e2e_can_be_set():
    engine = _setup_db()

    with Session(engine) as session:
        task = Task(id="task-e2e2", project_id="proj-test", title="Complex task", need_e2e=True)
        session.add(task)
        session.commit()
        session.refresh(task)

        assert task.need_e2e is True


def test_task_plan_defaults_to_none():
    engine = _setup_db()

    with Session(engine) as session:
        task = Task(id="task-noplan", project_id="proj-test", title="No plan")
        session.add(task)
        session.commit()
        session.refresh(task)

        assert task.plan is None


def test_task_with_project_id():
    engine = _setup_db()

    with Session(engine) as session:
        session.execute(
            insert(Task).values(
                id="task-proj",
                project_id="proj-123",
                title="Feature for project",
                type="feature",
                status="backlog",
                priority=1,
                current_iteration=0,
                max_iterations=3,
                created_by="user",
            )
        )
        session.commit()

    with Session(engine) as session:
        task = session.execute(select(Task).where(Task.id == "task-proj")).scalar_one()
        assert task.project_id == "proj-123"
        assert task.priority == 1


def test_task_blocked_by_task_id():
    engine = _setup_db()

    with Session(engine) as session:
        # Create blocker task
        session.execute(
            insert(Task).values(
                id="task-blocker",
                project_id="proj-test",
                title="Blocker task",
                type="feature",
                status="in_dev",
                priority=0,
                current_iteration=0,
                max_iterations=3,
                created_by="system",
            )
        )
        # Create blocked task
        session.execute(
            insert(Task).values(
                id="task-blocked",
                project_id="proj-test",
                title="Blocked task",
                type="feature",
                status="blocked",
                priority=0,
                current_iteration=0,
                max_iterations=3,
                created_by="system",
                blocked_by_task_id="task-blocker",
            )
        )
        session.commit()

    with Session(engine) as session:
        task = session.execute(select(Task).where(Task.id == "task-blocked")).scalar_one()
        assert task.blocked_by_task_id == "task-blocker"
        assert task.status == "blocked"


def test_multiple_events_for_task():
    engine = _setup_db()

    with Session(engine) as session:
        session.execute(
            insert(Task).values(
                id="task-multi",
                project_id="proj-test",
                title="Multi-event test",
                type="feature",
                status="in_dev",
                priority=0,
                current_iteration=1,
                max_iterations=3,
                created_by="system",
            )
        )
        for from_s, to_s in [
            ("backlog", "todo"),
            ("todo", "in_dev"),
        ]:
            session.execute(
                insert(TaskEvent).values(
                    task_id="task-multi",
                    event_type="status_change",
                    from_status=from_s,
                    to_status=to_s,
                    actor="system",
                    details={},
                )
            )
        session.execute(
            insert(TaskEvent).values(
                task_id="task-multi",
                event_type="iteration_start",
                iteration=0,
                actor="system",
                details={"run_id": "eng-111"},
            )
        )
        session.commit()

    with Session(engine) as session:
        events = (
            session.execute(
                select(TaskEvent).where(TaskEvent.task_id == "task-multi").order_by(TaskEvent.id)
            )
            .scalars()
            .all()
        )
        assert len(events) == 3
        assert events[0].event_type == "status_change"
        assert events[2].event_type == "iteration_start"
        assert events[2].iteration == 0
        assert events[2].details == {"run_id": "eng-111"}
