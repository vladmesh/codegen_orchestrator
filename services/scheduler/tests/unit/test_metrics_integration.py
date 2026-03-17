"""Integration tests: raw Prometheus text → parse → extract → structured output."""

from pathlib import Path

import pytest

from src.metrics.cadvisor import extract_container_metrics
from src.metrics.node_exporter import NodeMetrics, extract_node_metrics
from src.metrics.parser import parse_prometheus_text

FIXTURES = Path(__file__).parent.parent / "fixtures"


class TestNodeExporterEndToEnd:
    """Parse real node_exporter output and verify structured extraction."""

    @pytest.fixture
    def node_metrics(self) -> NodeMetrics:
        text = (FIXTURES / "node_exporter_sample.txt").read_text()
        parsed = parse_prometheus_text(text)
        return extract_node_metrics(parsed)

    def test_all_fields_populated(self, node_metrics: NodeMetrics):
        """Every field should be non-None with real data."""
        assert node_metrics.cpu_usage_pct is not None
        assert node_metrics.ram_used_bytes is not None
        assert node_metrics.ram_total_bytes is not None
        assert node_metrics.disk_used_bytes is not None
        assert node_metrics.disk_total_bytes is not None
        assert node_metrics.load_avg_1m is not None
        assert node_metrics.load_avg_5m is not None
        assert node_metrics.load_avg_15m is not None
        assert node_metrics.uptime_seconds is not None
        assert node_metrics.network_rx_errors is not None
        assert node_metrics.network_tx_errors is not None

    def test_cpu_usage_reasonable(self, node_metrics: NodeMetrics):
        """CPU usage should be between 0-100%."""
        assert 0.0 <= node_metrics.cpu_usage_pct <= 100.0

    def test_ram_values_consistent(self, node_metrics: NodeMetrics):
        """Used RAM < total RAM."""
        assert node_metrics.ram_used_bytes < node_metrics.ram_total_bytes
        assert node_metrics.ram_total_bytes == pytest.approx(4_123_738_112, rel=1e-6)

    def test_disk_values_consistent(self, node_metrics: NodeMetrics):
        """Used disk < total disk, total ~51GB."""
        assert node_metrics.disk_used_bytes < node_metrics.disk_total_bytes
        assert node_metrics.disk_total_bytes == pytest.approx(51_472_908_288, rel=1e-6)

    def test_load_averages(self, node_metrics: NodeMetrics):
        assert node_metrics.load_avg_1m == pytest.approx(0.68)
        assert node_metrics.load_avg_5m == pytest.approx(0.52)
        assert node_metrics.load_avg_15m == pytest.approx(0.41)

    def test_network_errors(self, node_metrics: NodeMetrics):
        assert node_metrics.network_rx_errors == 0
        assert node_metrics.network_tx_errors == 2


class TestCadvisorEndToEnd:
    """Parse real cadvisor output and verify per-container extraction."""

    @pytest.fixture
    def containers(self):
        text = (FIXTURES / "cadvisor_sample.txt").read_text()
        parsed = parse_prometheus_text(text)
        return extract_container_metrics(parsed)

    def test_correct_container_count(self, containers):
        """Should find 3 real containers, filtering out root and system."""
        assert len(containers) == 3

    def test_container_names(self, containers):
        """Containers sorted alphabetically."""
        names = [c.name for c in containers]
        assert names == ["myapp", "postgres", "redis"]

    def test_myapp_metrics(self, containers):
        myapp = containers[0]
        assert myapp.name == "myapp"
        assert myapp.cpu_usage_seconds == pytest.approx(345.67)
        assert myapp.memory_usage_bytes == 134_217_728
        assert myapp.memory_limit_bytes == 536_870_912
        assert myapp.network_rx_bytes == 5_678_900
        assert myapp.network_tx_bytes == 4_567_800

    def test_redis_zero_memory_limit(self, containers):
        """Redis has no memory limit (0)."""
        redis = next(c for c in containers if c.name == "redis")
        assert redis.memory_limit_bytes == 0

    def test_all_containers_have_cpu(self, containers):
        for c in containers:
            assert c.cpu_usage_seconds is not None
            assert c.cpu_usage_seconds > 0


class TestPublicAPI:
    """Test the convenience functions from src.metrics."""

    def test_parse_node_exporter(self):
        from src.metrics import parse_node_exporter

        text = (FIXTURES / "node_exporter_sample.txt").read_text()
        result = parse_node_exporter(text)
        assert isinstance(result, NodeMetrics)
        assert result.cpu_usage_pct is not None

    def test_parse_cadvisor(self):
        from src.metrics import parse_cadvisor

        text = (FIXTURES / "cadvisor_sample.txt").read_text()
        result = parse_cadvisor(text)
        assert len(result) == 3
        assert result[0].name == "myapp"
