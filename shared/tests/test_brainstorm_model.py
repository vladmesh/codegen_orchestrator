"""Unit tests for Brainstorm SQLAlchemy model."""

from shared.models.brainstorm import Brainstorm


def test_brainstorm_tablename():
    assert Brainstorm.__tablename__ == "brainstorms"


def test_brainstorm_columns():
    col_names = {c.name for c in Brainstorm.__table__.columns}
    expected = {
        "id",
        "project_id",
        "title",
        "content",
        "status",
        "created_by",
        "created_at",
        "updated_at",
    }
    assert expected.issubset(col_names)


def test_brainstorm_id_is_primary_key():
    pk_cols = [c.name for c in Brainstorm.__table__.primary_key.columns]
    assert pk_cols == ["id"]


def test_brainstorm_default_status():
    col = Brainstorm.__table__.columns["status"]
    assert col.default.arg == "draft"


def test_brainstorm_default_created_by():
    col = Brainstorm.__table__.columns["created_by"]
    assert col.default.arg == "system"
