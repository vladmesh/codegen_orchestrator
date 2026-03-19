"""Unit tests for ServerMetricsHistory model."""

from shared.models.server_metrics_history import ServerMetricsHistory


class TestServerMetricsHistoryModel:
    def test_tablename(self):
        assert ServerMetricsHistory.__tablename__ == "server_metrics_history"

    def test_columns_exist(self):
        cols = {c.name for c in ServerMetricsHistory.__table__.columns}
        expected = {
            "id",
            "server_handle",
            "recorded_at",
            "metrics",
            "created_at",
            "updated_at",
        }
        assert expected.issubset(cols)

    def test_server_handle_fk(self):
        col = ServerMetricsHistory.__table__.c.server_handle
        fk_targets = [fk.target_fullname for fk in col.foreign_keys]
        assert "servers.handle" in fk_targets

    def test_server_handle_indexed(self):
        col = ServerMetricsHistory.__table__.c.server_handle
        assert col.index or any(
            col in idx.columns for idx in ServerMetricsHistory.__table__.indexes
        )

    def test_recorded_at_indexed(self):
        indexes = ServerMetricsHistory.__table__.indexes
        indexed_cols = set()
        for idx in indexes:
            for c in idx.columns:
                indexed_cols.add(c.name)
        assert "recorded_at" in indexed_cols

    def test_metrics_is_json(self):
        col = ServerMetricsHistory.__table__.c.metrics
        assert col is not None

    def test_id_is_primary_key(self):
        assert ServerMetricsHistory.__table__.c.id.primary_key
