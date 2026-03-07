"""Unit tests for WorkItem and WorkItemEvent models + enums + transition matrix."""

from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import Session

from shared.contracts.dto.work_item import (
    VALID_TRANSITIONS,
    WorkItemEventType,
    WorkItemStatus,
    WorkItemType,
)
from shared.models.work_item import WorkItem, WorkItemEvent


def _setup_db():
    """Create in-memory SQLite DB with work_items and work_item_events tables."""
    engine = create_engine("sqlite:///:memory:")
    WorkItem.__table__.create(engine)
    WorkItemEvent.__table__.create(engine)
    return engine


# --- Enum tests ---


def test_work_item_status_values():
    assert WorkItemStatus.BACKLOG == "backlog"
    assert WorkItemStatus.TODO == "todo"
    assert WorkItemStatus.IN_DEV == "in_dev"
    assert WorkItemStatus.DONE == "done"
    assert WorkItemStatus.FAILED == "failed"
    assert WorkItemStatus.CANCELLED == "cancelled"


def test_work_item_type_values():
    assert WorkItemType.CREATE == "create"
    assert WorkItemType.FEATURE == "feature"
    assert WorkItemType.FIX == "fix"
    assert WorkItemType.REFACTOR == "refactor"


def test_work_item_event_type_values():
    assert WorkItemEventType.STATUS_CHANGE == "status_change"
    assert WorkItemEventType.ITERATION_START == "iteration_start"
    assert WorkItemEventType.ITERATION_END == "iteration_end"
    assert WorkItemEventType.NOTE == "note"
    assert WorkItemEventType.STEP_START == "step_start"
    assert WorkItemEventType.STEP_DONE == "step_done"


# --- Transition matrix tests ---


def test_backlog_can_go_to_todo():
    assert WorkItemStatus.TODO in VALID_TRANSITIONS[WorkItemStatus.BACKLOG]


def test_backlog_cannot_go_to_done():
    assert WorkItemStatus.DONE not in VALID_TRANSITIONS[WorkItemStatus.BACKLOG]


def test_done_can_reopen_to_backlog():
    assert WorkItemStatus.BACKLOG in VALID_TRANSITIONS[WorkItemStatus.DONE]


def test_cancelled_is_terminal():
    assert VALID_TRANSITIONS[WorkItemStatus.CANCELLED] == set()


def test_all_statuses_have_transitions():
    for status in WorkItemStatus:
        assert status in VALID_TRANSITIONS, f"{status} missing from VALID_TRANSITIONS"


def test_in_dev_can_go_to_testing_or_review():
    allowed = VALID_TRANSITIONS[WorkItemStatus.IN_DEV]
    assert WorkItemStatus.TESTING in allowed
    assert WorkItemStatus.IN_REVIEW in allowed


def test_testing_can_return_to_in_dev():
    assert WorkItemStatus.IN_DEV in VALID_TRANSITIONS[WorkItemStatus.TESTING]


# --- Model instantiation tests ---


def test_work_item_defaults():
    engine = _setup_db()

    with Session(engine) as session:
        wi = WorkItem(id="wi-test1", project_id="proj-test", title="Test feature")
        session.add(wi)
        session.commit()
        session.refresh(wi)

        assert wi.status == WorkItemStatus.BACKLOG.value
        assert wi.type == WorkItemType.FEATURE.value
        assert wi.priority == 0
        assert wi.current_iteration == 0
        assert wi.max_iterations == 3
        assert wi.created_by == "system"


def test_work_item_persist_and_read():
    engine = _setup_db()

    with Session(engine) as session:
        session.execute(
            insert(WorkItem).values(
                id="wi-abc",
                project_id="proj-test",
                title="Add statistics button",
                description="Full description here",
                type=WorkItemType.FEATURE.value,
                status=WorkItemStatus.BACKLOG.value,
                priority=0,
                current_iteration=0,
                max_iterations=3,
                created_by="po",
            )
        )
        session.commit()

    with Session(engine) as session:
        wi = session.execute(select(WorkItem).where(WorkItem.id == "wi-abc")).scalar_one()
        assert wi.title == "Add statistics button"
        assert wi.description == "Full description here"
        assert wi.type == WorkItemType.FEATURE.value
        assert wi.status == WorkItemStatus.BACKLOG.value
        assert wi.created_by == "po"


def test_work_item_event_persist_and_read():
    engine = _setup_db()

    with Session(engine) as session:
        session.execute(
            insert(WorkItem).values(
                id="wi-abc",
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
            insert(WorkItemEvent).values(
                work_item_id="wi-abc",
                event_type=WorkItemEventType.STATUS_CHANGE.value,
                from_status=WorkItemStatus.BACKLOG.value,
                to_status=WorkItemStatus.TODO.value,
                actor="po",
                details={},
            )
        )
        session.commit()

    with Session(engine) as session:
        event = session.execute(
            select(WorkItemEvent).where(WorkItemEvent.work_item_id == "wi-abc")
        ).scalar_one()
        assert event.event_type == WorkItemEventType.STATUS_CHANGE.value
        assert event.from_status == WorkItemStatus.BACKLOG.value
        assert event.to_status == WorkItemStatus.TODO.value
        assert event.actor == "po"


def test_work_item_with_project_id():
    engine = _setup_db()

    with Session(engine) as session:
        session.execute(
            insert(WorkItem).values(
                id="wi-proj",
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
        wi = session.execute(select(WorkItem).where(WorkItem.id == "wi-proj")).scalar_one()
        assert wi.project_id == "proj-123"
        assert wi.priority == 1


def test_multiple_events_for_work_item():
    engine = _setup_db()

    with Session(engine) as session:
        session.execute(
            insert(WorkItem).values(
                id="wi-multi",
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
                insert(WorkItemEvent).values(
                    work_item_id="wi-multi",
                    event_type="status_change",
                    from_status=from_s,
                    to_status=to_s,
                    actor="system",
                    details={},
                )
            )
        session.execute(
            insert(WorkItemEvent).values(
                work_item_id="wi-multi",
                event_type="iteration_start",
                iteration=0,
                actor="system",
                details={"task_id": "eng-111"},
            )
        )
        session.commit()

    with Session(engine) as session:
        events = (
            session.execute(
                select(WorkItemEvent)
                .where(WorkItemEvent.work_item_id == "wi-multi")
                .order_by(WorkItemEvent.id)
            )
            .scalars()
            .all()
        )
        assert len(events) == 3
        assert events[0].event_type == "status_change"
        assert events[2].event_type == "iteration_start"
        assert events[2].iteration == 0
        assert events[2].details == {"task_id": "eng-111"}
