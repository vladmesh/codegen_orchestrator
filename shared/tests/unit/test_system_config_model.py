"""Unit tests for SystemConfig model."""

from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import Session

from shared.models.system_config import SystemConfig


def _setup_db():
    """Create in-memory SQLite DB with system_configs table."""
    engine = create_engine("sqlite:///:memory:")
    SystemConfig.__table__.create(engine)
    return engine


def test_system_config_tablename():
    assert SystemConfig.__tablename__ == "system_configs"


def test_system_config_create_and_read():
    engine = _setup_db()

    with Session(engine) as session:
        session.execute(
            insert(SystemConfig).values(
                key="scheduler.dispatch_interval_seconds",
                value=30,
                description="Task dispatcher poll interval in seconds",
                category="scheduler",
                updated_by="seed",
            )
        )
        session.commit()

    with Session(engine) as session:
        cfg = session.execute(
            select(SystemConfig).where(SystemConfig.key == "scheduler.dispatch_interval_seconds")
        ).scalar_one()
        assert cfg.value == 30
        assert cfg.description == "Task dispatcher poll interval in seconds"
        assert cfg.category == "scheduler"
        assert cfg.updated_by == "seed"


def test_system_config_json_value_types():
    """Value column supports int, float, string, bool, dict, list."""
    engine = _setup_db()

    cases = [
        ("int_val", 42, "test"),
        ("float_val", 90.5, "test"),
        ("str_val", "hello", "test"),
        ("bool_val", True, "test"),
        ("dict_val", {"nested": "value"}, "test"),
        ("list_val", [1, 2, 3], "test"),
    ]

    with Session(engine) as session:
        for key, value, category in cases:
            session.execute(insert(SystemConfig).values(key=key, value=value, category=category))
        session.commit()

    with Session(engine) as session:
        for key, expected_value, _ in cases:
            cfg = session.execute(select(SystemConfig).where(SystemConfig.key == key)).scalar_one()
            assert cfg.value == expected_value, f"Failed for key={key}"


def test_system_config_nullable_fields():
    """description and updated_by are nullable."""
    engine = _setup_db()

    with Session(engine) as session:
        session.execute(
            insert(SystemConfig).values(
                key="minimal",
                value=1,
                category="test",
            )
        )
        session.commit()

    with Session(engine) as session:
        cfg = session.execute(
            select(SystemConfig).where(SystemConfig.key == "minimal")
        ).scalar_one()
        assert cfg.description is None
        assert cfg.updated_by is None


def test_system_config_filter_by_category():
    engine = _setup_db()

    with Session(engine) as session:
        for key, cat in [
            ("sched.a", "scheduler"),
            ("sched.b", "scheduler"),
            ("health.a", "health"),
        ]:
            session.execute(insert(SystemConfig).values(key=key, value=1, category=cat))
        session.commit()

    with Session(engine) as session:
        results = (
            session.execute(select(SystemConfig).where(SystemConfig.category == "scheduler"))
            .scalars()
            .all()
        )
        assert len(results) == 2


def test_system_config_repr():
    cfg = SystemConfig(key="test.key", value=1, category="test")
    assert "test.key" in repr(cfg)
    assert "test" in repr(cfg)
