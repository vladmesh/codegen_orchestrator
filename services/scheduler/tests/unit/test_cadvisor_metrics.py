"""Tests for cadvisor metric extraction."""

from src.metrics.cadvisor import extract_container_metrics
from src.metrics.parser import PrometheusMetric


def _m(name: str, value: float, labels: dict | None = None) -> PrometheusMetric:
    """Shortcut to build a PrometheusMetric."""
    return PrometheusMetric(name=name, labels=labels or {}, value=value)


# Common cadvisor label sets
_APP_LABELS = {"id": "/docker/abc123", "name": "myapp", "image": "myapp:latest"}
_DB_LABELS = {"id": "/docker/def456", "name": "postgres", "image": "postgres:16"}
_POD_LABELS = {"id": "/", "name": ""}
_SYSTEM_LABELS = {"id": "/system.slice/docker.service", "name": ""}


class TestExtractContainerMetrics:
    """Test cadvisor per-container metric extraction."""

    def test_empty_input(self):
        assert extract_container_metrics([]) == []

    def test_single_container(self):
        metrics = [
            _m("container_cpu_usage_seconds_total", 123.45, _APP_LABELS),
            _m("container_memory_usage_bytes", 100_000_000.0, _APP_LABELS),
            _m("container_spec_memory_limit_bytes", 512_000_000.0, _APP_LABELS),
            _m("container_network_receive_bytes_total", 5000.0, _APP_LABELS),
            _m("container_network_transmit_bytes_total", 3000.0, _APP_LABELS),
        ]
        result = extract_container_metrics(metrics)
        assert len(result) == 1
        c = result[0]
        assert c.name == "myapp"
        assert c.cpu_usage_seconds == 123.45
        assert c.memory_usage_bytes == 100_000_000
        assert c.memory_limit_bytes == 512_000_000
        assert c.network_rx_bytes == 5000
        assert c.network_tx_bytes == 3000

    def test_multiple_containers(self):
        metrics = [
            _m("container_cpu_usage_seconds_total", 100.0, _APP_LABELS),
            _m("container_cpu_usage_seconds_total", 200.0, _DB_LABELS),
            _m("container_memory_usage_bytes", 50_000.0, _APP_LABELS),
            _m("container_memory_usage_bytes", 80_000.0, _DB_LABELS),
        ]
        result = extract_container_metrics(metrics)
        assert len(result) == 2
        names = {c.name for c in result}
        assert names == {"myapp", "postgres"}

    def test_filters_out_pod_and_system_containers(self):
        """Containers with empty name or root/system IDs are filtered out."""
        metrics = [
            _m("container_cpu_usage_seconds_total", 100.0, _APP_LABELS),
            _m("container_cpu_usage_seconds_total", 999.0, _POD_LABELS),
            _m("container_cpu_usage_seconds_total", 888.0, _SYSTEM_LABELS),
        ]
        result = extract_container_metrics(metrics)
        assert len(result) == 1
        assert result[0].name == "myapp"

    def test_cgroup_v2_docker_containers_included(self):
        """cgroup v2 containers with /system.slice/docker-*.scope IDs are real containers."""
        cgroupv2_labels = {
            "id": "/system.slice/docker-98ae5a1c48007463fbc20b6c1507ff499c69ab41.scope",
            "name": "fortune-teller-bot-db-1",
        }
        metrics = [
            _m("container_cpu_usage_seconds_total", 431.5, cgroupv2_labels),
            _m("container_memory_usage_bytes", 31219712.0, cgroupv2_labels),
        ]
        result = extract_container_metrics(metrics)
        assert len(result) == 1
        assert result[0].name == "fortune-teller-bot-db-1"
        assert result[0].cpu_usage_seconds == 431.5

    def test_missing_optional_fields(self):
        """Container with only CPU metric — other fields default to None."""
        metrics = [
            _m("container_cpu_usage_seconds_total", 50.0, _APP_LABELS),
        ]
        result = extract_container_metrics(metrics)
        assert len(result) == 1
        c = result[0]
        assert c.cpu_usage_seconds == 50.0
        assert c.memory_usage_bytes is None
        assert c.memory_limit_bytes is None
        assert c.network_rx_bytes is None
        assert c.network_tx_bytes is None

    def test_sorted_by_name(self):
        """Results are sorted by container name for stable output."""
        labels_b = {"id": "/docker/bbb", "name": "beta", "image": "beta:1"}
        labels_a = {"id": "/docker/aaa", "name": "alpha", "image": "alpha:1"}
        metrics = [
            _m("container_cpu_usage_seconds_total", 1.0, labels_b),
            _m("container_cpu_usage_seconds_total", 2.0, labels_a),
        ]
        result = extract_container_metrics(metrics)
        assert [c.name for c in result] == ["alpha", "beta"]

    def test_container_with_zero_memory_limit(self):
        """Zero memory limit means unlimited — should be stored as 0."""
        labels = {"id": "/docker/xxx", "name": "unlimited", "image": "img:1"}
        metrics = [
            _m("container_memory_usage_bytes", 100.0, labels),
            _m("container_spec_memory_limit_bytes", 0.0, labels),
        ]
        result = extract_container_metrics(metrics)
        assert result[0].memory_limit_bytes == 0
