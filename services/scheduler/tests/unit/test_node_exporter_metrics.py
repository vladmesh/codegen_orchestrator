"""Tests for node_exporter metric extraction."""

import pytest

from src.metrics.node_exporter import extract_node_metrics
from src.metrics.parser import PrometheusMetric


def _m(name: str, value: float, labels: dict | None = None) -> PrometheusMetric:
    """Shortcut to build a PrometheusMetric."""
    return PrometheusMetric(name=name, labels=labels or {}, value=value)


class TestExtractNodeMetrics:
    """Test node_exporter metric extraction."""

    def test_empty_input(self):
        result = extract_node_metrics([])
        assert result.cpu_usage_pct is None
        assert result.ram_used_bytes is None
        assert result.ram_total_bytes is None

    def test_cpu_usage_from_idle_ratio(self):
        """CPU usage = 100% - idle%. Two CPUs, each with idle + user + system."""
        metrics = [
            # CPU 0: idle=800, user=100, system=100 → total=1000, idle=80%
            _m("node_cpu_seconds_total", 800.0, {"cpu": "0", "mode": "idle"}),
            _m("node_cpu_seconds_total", 100.0, {"cpu": "0", "mode": "user"}),
            _m("node_cpu_seconds_total", 100.0, {"cpu": "0", "mode": "system"}),
            # CPU 1: idle=600, user=200, system=200 → total=1000, idle=60%
            _m("node_cpu_seconds_total", 600.0, {"cpu": "1", "mode": "idle"}),
            _m("node_cpu_seconds_total", 200.0, {"cpu": "1", "mode": "user"}),
            _m("node_cpu_seconds_total", 200.0, {"cpu": "1", "mode": "system"}),
        ]
        result = extract_node_metrics(metrics)
        # Average idle = (80% + 60%) / 2 = 70%, usage = 30%
        assert result.cpu_usage_pct == pytest.approx(30.0, abs=0.1)

    def test_ram_metrics(self):
        metrics = [
            _m("node_memory_MemTotal_bytes", 4_294_967_296.0),
            _m("node_memory_MemAvailable_bytes", 2_147_483_648.0),
        ]
        result = extract_node_metrics(metrics)
        assert result.ram_total_bytes == 4_294_967_296
        assert result.ram_used_bytes == 2_147_483_648  # total - available

    def test_disk_metrics_root_mountpoint(self):
        """Only root (/) mountpoint is used for disk metrics."""
        metrics = [
            _m(
                "node_filesystem_size_bytes",
                50_000_000_000.0,
                {"mountpoint": "/", "fstype": "ext4"},
            ),
            _m(
                "node_filesystem_avail_bytes",
                30_000_000_000.0,
                {"mountpoint": "/", "fstype": "ext4"},
            ),
            # Another mountpoint — should be ignored
            _m(
                "node_filesystem_size_bytes",
                100_000_000.0,
                {"mountpoint": "/boot", "fstype": "ext4"},
            ),
            _m(
                "node_filesystem_avail_bytes",
                90_000_000.0,
                {"mountpoint": "/boot", "fstype": "ext4"},
            ),
        ]
        result = extract_node_metrics(metrics)
        assert result.disk_total_bytes == 50_000_000_000
        assert result.disk_used_bytes == 20_000_000_000  # size - avail

    def test_load_averages(self):
        metrics = [
            _m("node_load1", 0.42),
            _m("node_load5", 0.35),
            _m("node_load15", 0.20),
        ]
        result = extract_node_metrics(metrics)
        assert result.load_avg_1m == pytest.approx(0.42)
        assert result.load_avg_5m == pytest.approx(0.35)
        assert result.load_avg_15m == pytest.approx(0.20)

    def test_uptime(self):
        metrics = [_m("node_boot_time_seconds", 1710000000.0)]
        result = extract_node_metrics(metrics)
        # uptime = now - boot_time; just check it's set and positive
        assert result.uptime_seconds is not None
        assert result.uptime_seconds > 0

    def test_network_errors(self):
        metrics = [
            _m("node_network_receive_errs_total", 5.0, {"device": "eth0"}),
            _m("node_network_transmit_errs_total", 3.0, {"device": "eth0"}),
            _m("node_network_receive_errs_total", 1.0, {"device": "lo"}),
            _m("node_network_transmit_errs_total", 0.0, {"device": "lo"}),
        ]
        result = extract_node_metrics(metrics)
        # Sum across all devices
        assert result.network_rx_errors == 6
        assert result.network_tx_errors == 3

    def test_missing_metrics_return_none(self):
        """When only partial metrics are available, missing fields are None."""
        metrics = [_m("node_load1", 0.42)]
        result = extract_node_metrics(metrics)
        assert result.load_avg_1m == pytest.approx(0.42)
        assert result.cpu_usage_pct is None
        assert result.ram_used_bytes is None
        assert result.disk_used_bytes is None
        assert result.uptime_seconds is None
        assert result.network_rx_errors is None

    def test_single_cpu(self):
        """Works with just one CPU."""
        metrics = [
            _m("node_cpu_seconds_total", 900.0, {"cpu": "0", "mode": "idle"}),
            _m("node_cpu_seconds_total", 50.0, {"cpu": "0", "mode": "user"}),
            _m("node_cpu_seconds_total", 50.0, {"cpu": "0", "mode": "system"}),
        ]
        result = extract_node_metrics(metrics)
        # idle=900/1000=90%, usage=10%
        assert result.cpu_usage_pct == pytest.approx(10.0, abs=0.1)
