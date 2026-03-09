"""Unit tests for Project model — MutableDict on JSON columns."""

import uuid

from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import Session

from shared.models.project import Project
from shared.models.user import User

TEST_PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _setup_db():
    """Create in-memory SQLite DB with only the tables we need."""
    engine = create_engine("sqlite:///:memory:")
    # Only create tables for User and Project to avoid TSVECTOR issues
    User.__table__.create(engine)
    Project.__table__.create(engine)
    return engine


def test_config_inplace_mutation_detected():
    """In-place dict mutation on Project.config must be detected and persisted."""
    engine = _setup_db()

    with Session(engine) as session:
        session.execute(insert(User).values(id=1, telegram_id=100, username="tester"))
        session.execute(
            insert(Project).values(
                id=TEST_PROJECT_ID,
                name="test",
                status="draft",
                config={"modules": ["backend"]},
                owner_id=1,
            )
        )
        session.commit()

    # Mutate config in-place and commit
    with Session(engine) as session:
        project = session.execute(select(Project).where(Project.id == TEST_PROJECT_ID)).scalar_one()
        project.config["secrets"] = {"API_KEY": "encrypted-value"}
        session.commit()

    # Re-read and verify
    with Session(engine) as session:
        project = session.execute(select(Project).where(Project.id == TEST_PROJECT_ID)).scalar_one()
        assert "secrets" in project.config, (
            "In-place mutation was not persisted — MutableDict.as_mutable(JSON) missing?"
        )
        assert project.config["secrets"] == {"API_KEY": "encrypted-value"}


def test_project_spec_inplace_mutation_detected():
    """In-place dict mutation on Project.project_spec must be detected and persisted."""
    engine = _setup_db()

    with Session(engine) as session:
        session.execute(insert(User).values(id=1, telegram_id=100, username="tester"))
        session.execute(
            insert(Project).values(
                id=TEST_PROJECT_ID,
                name="test",
                status="draft",
                config={},
                project_spec={"version": "1.0"},
                owner_id=1,
            )
        )
        session.commit()

    with Session(engine) as session:
        project = session.execute(select(Project).where(Project.id == TEST_PROJECT_ID)).scalar_one()
        project.project_spec["services"] = ["backend"]
        session.commit()

    with Session(engine) as session:
        project = session.execute(select(Project).where(Project.id == TEST_PROJECT_ID)).scalar_one()
        assert "services" in project.project_spec, "In-place mutation on project_spec not persisted"
