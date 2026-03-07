"""Unit tests for Milestone SQLAlchemy model."""

from shared.models.milestone import Milestone


def test_milestone_tablename():
    assert Milestone.__tablename__ == "milestones"


def test_milestone_columns():
    col_names = {c.name for c in Milestone.__table__.columns}
    expected = {
        "id",
        "project_id",
        "title",
        "description",
        "sort_order",
        "status",
        "parent_id",
        "created_by",
        "created_at",
        "updated_at",
    }
    assert expected.issubset(col_names)


def test_milestone_id_is_primary_key():
    pk_cols = [c.name for c in Milestone.__table__.primary_key.columns]
    assert pk_cols == ["id"]


def test_milestone_default_status():
    col = Milestone.__table__.columns["status"]
    assert col.default.arg == "open"


def test_milestone_default_created_by():
    col = Milestone.__table__.columns["created_by"]
    assert col.default.arg == "system"


def test_milestone_default_sort_order():
    col = Milestone.__table__.columns["sort_order"]
    assert col.default.arg == 0


def test_milestone_parent_id_nullable():
    col = Milestone.__table__.columns["parent_id"]
    assert col.nullable is True


def test_task_has_milestone_id():
    from shared.models.task import Task

    col_names = {c.name for c in Task.__table__.columns}
    assert "milestone_id" in col_names
    col = Task.__table__.columns["milestone_id"]
    assert col.nullable is True
