"""Unit tests for Server model — health metric columns."""

from shared.models.server import Server


class TestServerHealthMetricColumns:
    """Verify new health metric columns exist and are nullable."""

    def test_cpu_usage_pct_column(self):
        cols = {c.name for c in Server.__table__.columns}
        assert "cpu_usage_pct" in cols
        assert Server.__table__.c.cpu_usage_pct.nullable

    def test_load_avg_columns(self):
        cols = {c.name for c in Server.__table__.columns}
        for col_name in ("load_avg_1m", "load_avg_5m", "load_avg_15m"):
            assert col_name in cols, f"{col_name} missing"
            assert getattr(Server.__table__.c, col_name).nullable

    def test_network_error_columns(self):
        cols = {c.name for c in Server.__table__.columns}
        for col_name in ("network_rx_errors", "network_tx_errors"):
            assert col_name in cols, f"{col_name} missing"
            assert getattr(Server.__table__.c, col_name).nullable

    def test_container_count_columns(self):
        cols = {c.name for c in Server.__table__.columns}
        for col_name in ("container_count_running", "container_count_total"):
            assert col_name in cols, f"{col_name} missing"
            assert getattr(Server.__table__.c, col_name).nullable

    def test_uptime_seconds_column(self):
        cols = {c.name for c in Server.__table__.columns}
        assert "uptime_seconds" in cols
        assert Server.__table__.c.uptime_seconds.nullable

    def test_existing_last_health_check_column(self):
        """last_health_check already exists and should remain nullable."""
        cols = {c.name for c in Server.__table__.columns}
        assert "last_health_check" in cols
        assert Server.__table__.c.last_health_check.nullable
