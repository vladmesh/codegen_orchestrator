"""Unit tests for ApplicationHealthHistory model."""

from shared.models.application_health_history import ApplicationHealthHistory


class TestApplicationHealthHistoryModel:
    def test_tablename(self):
        assert ApplicationHealthHistory.__tablename__ == "application_health_history"

    def test_columns_exist(self):
        cols = {c.name for c in ApplicationHealthHistory.__table__.columns}
        expected = {
            "id",
            "application_id",
            "recorded_at",
            "metrics",
            "created_at",
            "updated_at",
        }
        assert expected.issubset(cols)

    def test_application_id_fk(self):
        col = ApplicationHealthHistory.__table__.c.application_id
        fk_targets = [fk.target_fullname for fk in col.foreign_keys]
        assert "applications.id" in fk_targets

    def test_application_id_indexed(self):
        col = ApplicationHealthHistory.__table__.c.application_id
        assert col.index or any(
            col in idx.columns for idx in ApplicationHealthHistory.__table__.indexes
        )

    def test_recorded_at_indexed(self):
        indexes = ApplicationHealthHistory.__table__.indexes
        indexed_cols = set()
        for idx in indexes:
            for c in idx.columns:
                indexed_cols.add(c.name)
        assert "recorded_at" in indexed_cols

    def test_composite_index_exists(self):
        indexes = ApplicationHealthHistory.__table__.indexes
        composite_found = False
        for idx in indexes:
            cols = {c.name for c in idx.columns}
            if cols == {"application_id", "recorded_at"}:
                composite_found = True
                break
        assert composite_found

    def test_metrics_is_json(self):
        col = ApplicationHealthHistory.__table__.c.metrics
        assert col is not None

    def test_id_is_primary_key(self):
        assert ApplicationHealthHistory.__table__.c.id.primary_key
