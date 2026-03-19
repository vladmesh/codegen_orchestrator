"""Unit tests for Server DTOs — health metric fields."""

from datetime import UTC, datetime

from shared.contracts.dto.server import ServerDTO, ServerMetricsHistoryDTO, ServerUpdate

_NOW = datetime(2026, 3, 17, tzinfo=UTC)
_BASE_FIELDS = {
    "handle": "srv-1",
    "host": "host",
    "public_ip": "1.2.3.4",
    "status": "active",
    "is_managed": True,
    "created_at": _NOW,
}


class TestServerDTOHealthFields:
    """ServerDTO should include all health metric fields with None defaults."""

    def test_health_fields_default_none(self):
        dto = ServerDTO(**_BASE_FIELDS)
        assert dto.cpu_usage_pct is None
        assert dto.load_avg_1m is None
        assert dto.load_avg_5m is None
        assert dto.load_avg_15m is None
        assert dto.network_rx_errors is None
        assert dto.network_tx_errors is None
        assert dto.container_count_running is None
        assert dto.container_count_total is None
        assert dto.uptime_seconds is None

    def test_health_fields_populated(self):
        dto = ServerDTO(
            **_BASE_FIELDS,
            cpu_usage_pct=42.5,
            load_avg_1m=1.2,
            load_avg_5m=0.8,
            load_avg_15m=0.5,
            network_rx_errors=10,
            network_tx_errors=3,
            container_count_running=5,
            container_count_total=7,
            uptime_seconds=86400.0,
        )
        assert dto.cpu_usage_pct == 42.5
        assert dto.container_count_running == 5


class TestServerUpdateHealthFields:
    """ServerUpdate should accept health metric fields."""

    def test_health_fields_optional(self):
        update = ServerUpdate()
        assert update.cpu_usage_pct is None
        assert update.container_count_running is None

    def test_set_health_fields(self):
        update = ServerUpdate(cpu_usage_pct=55.0, uptime_seconds=3600.0)
        assert update.cpu_usage_pct == 55.0
        assert update.uptime_seconds == 3600.0


class TestServerMetricsHistoryDTO:
    """ServerMetricsHistoryDTO should carry server_handle, recorded_at, metrics."""

    def test_construction(self):
        dto = ServerMetricsHistoryDTO(
            server_handle="srv-1",
            recorded_at=_NOW,
            metrics={"cpu_usage_pct": 42.5, "load_avg_1m": 1.2},
        )
        assert dto.server_handle == "srv-1"
        assert dto.metrics["cpu_usage_pct"] == 42.5

    def test_id_optional(self):
        dto = ServerMetricsHistoryDTO(
            server_handle="srv-1",
            recorded_at=_NOW,
            metrics={},
        )
        assert dto.id is None
