"""Unit tests for analytics models — table structure, indexes, constraints."""

from shared.models.analytics_daily import AnalyticsDaily
from shared.models.analytics_hourly import AnalyticsHourly
from shared.models.analytics_known_users import AnalyticsKnownUsers

# --- AnalyticsHourly ---


def test_analytics_hourly_tablename():
    assert AnalyticsHourly.__tablename__ == "analytics_hourly"


def test_analytics_hourly_has_unique_constraint_and_index():
    index_names = {idx.name for idx in AnalyticsHourly.__table__.indexes}
    assert "ix_analytics_hourly_project_bucket" in index_names

    constraint_names = {
        c.name
        for c in AnalyticsHourly.__table__.constraints
        if hasattr(c, "columns") and len(c.columns) > 1
    }
    assert "uq_analytics_hourly_project_service_bucket" in constraint_names


def test_analytics_hourly_columns():
    col_names = {c.name for c in AnalyticsHourly.__table__.columns}
    expected = {
        "id",
        "project_id",
        "service_name",
        "bucket",
        "total_requests",
        "error_count",
        "unique_users",
        "new_users",
        "p50_ms",
        "p95_ms",
        "p99_ms",
        "top_endpoints",
        "created_at",
        "updated_at",
    }
    assert expected.issubset(col_names)


def test_analytics_hourly_project_id_fk():
    fks = AnalyticsHourly.__table__.columns["project_id"].foreign_keys
    assert len(fks) == 1
    fk = next(iter(fks))
    assert fk.target_fullname == "projects.id"


# --- AnalyticsDaily ---


def test_analytics_daily_tablename():
    assert AnalyticsDaily.__tablename__ == "analytics_daily"


def test_analytics_daily_has_unique_constraint():
    constraint_names = {
        c.name
        for c in AnalyticsDaily.__table__.constraints
        if hasattr(c, "columns") and len(c.columns) > 1
    }
    assert "uq_analytics_daily_project_date" in constraint_names


def test_analytics_daily_columns():
    col_names = {c.name for c in AnalyticsDaily.__table__.columns}
    expected = {
        "id",
        "project_id",
        "date",
        "total_requests",
        "error_count",
        "unique_users",
        "new_users",
        "dau",
        "returning_users",
        "p95_ms",
        "error_rate",
        "created_at",
        "updated_at",
    }
    assert expected.issubset(col_names)


# --- AnalyticsKnownUsers ---


def test_analytics_known_users_tablename():
    assert AnalyticsKnownUsers.__tablename__ == "analytics_known_users"


def test_analytics_known_users_composite_pk():
    pk_cols = {c.name for c in AnalyticsKnownUsers.__table__.primary_key.columns}
    assert pk_cols == {"project_id", "user_id_hash"}


def test_analytics_known_users_columns():
    col_names = {c.name for c in AnalyticsKnownUsers.__table__.columns}
    expected = {
        "project_id",
        "user_id_hash",
        "first_seen",
        "last_seen",
        "created_at",
        "updated_at",
    }
    assert expected.issubset(col_names)


def test_analytics_known_users_project_id_fk():
    fks = AnalyticsKnownUsers.__table__.columns["project_id"].foreign_keys
    assert len(fks) == 1
    fk = next(iter(fks))
    assert fk.target_fullname == "projects.id"
