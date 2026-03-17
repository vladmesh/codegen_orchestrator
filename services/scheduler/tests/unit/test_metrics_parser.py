"""Tests for generic Prometheus text format parser."""

import pytest

from src.metrics.parser import PrometheusMetric, parse_prometheus_text


class TestParsePrometheusText:
    """Test Prometheus exposition format parsing."""

    def test_empty_input(self):
        assert parse_prometheus_text("") == []

    def test_only_comments(self):
        text = (
            "# HELP node_cpu_seconds_total Seconds the CPUs spent in each mode.\n"
            "# TYPE node_cpu_seconds_total counter\n"
        )
        assert parse_prometheus_text(text) == []

    def test_simple_gauge_no_labels(self):
        text = "node_load1 0.42\n"
        result = parse_prometheus_text(text)
        assert len(result) == 1
        assert result[0] == PrometheusMetric(name="node_load1", labels={}, value=0.42)

    def test_metric_with_labels(self):
        text = 'node_cpu_seconds_total{cpu="0",mode="idle"} 12345.67\n'
        result = parse_prometheus_text(text)
        assert len(result) == 1
        m = result[0]
        assert m.name == "node_cpu_seconds_total"
        assert m.labels == {"cpu": "0", "mode": "idle"}
        assert m.value == 12345.67

    def test_metric_with_timestamp(self):
        text = "http_requests_total 1027 1395066363000\n"
        result = parse_prometheus_text(text)
        assert len(result) == 1
        assert result[0].value == 1027.0
        assert result[0].timestamp == 1395066363000.0

    def test_multiple_metrics_mixed(self):
        text = (
            "# HELP node_load1 1m load average.\n"
            "# TYPE node_load1 gauge\n"
            "node_load1 0.42\n"
            "# HELP node_load5 5m load average.\n"
            "# TYPE node_load5 gauge\n"
            "node_load5 0.35\n"
            "node_load15 0.20\n"
        )
        result = parse_prometheus_text(text)
        assert len(result) == 3
        names = [m.name for m in result]
        assert names == ["node_load1", "node_load5", "node_load15"]

    def test_blank_lines_ignored(self):
        text = "\n\nnode_load1 0.42\n\n\nnode_load5 0.35\n\n"
        result = parse_prometheus_text(text)
        assert len(result) == 2

    def test_scientific_notation(self):
        text = "some_metric 1.23e+4\n"
        result = parse_prometheus_text(text)
        assert result[0].value == 12300.0

    def test_inf_and_nan(self):
        text = 'bucket_le_inf{le="+Inf"} 42\nsome_nan NaN\n'
        result = parse_prometheus_text(text)
        assert result[0].value == 42.0
        assert result[1].value != result[1].value  # NaN != NaN

    def test_multiline_node_exporter_snippet(self):
        """Parse a realistic node_exporter snippet."""
        text = (
            "# HELP node_cpu_seconds_total Seconds the CPUs spent in each mode.\n"
            "# TYPE node_cpu_seconds_total counter\n"
            'node_cpu_seconds_total{cpu="0",mode="idle"} 78945.23\n'
            'node_cpu_seconds_total{cpu="0",mode="system"} 1234.56\n'
            'node_cpu_seconds_total{cpu="0",mode="user"} 5678.90\n'
            'node_cpu_seconds_total{cpu="1",mode="idle"} 79000.11\n'
            'node_cpu_seconds_total{cpu="1",mode="system"} 1100.22\n'
            'node_cpu_seconds_total{cpu="1",mode="user"} 5500.33\n'
            "# HELP node_memory_MemTotal_bytes Memory information field MemTotal_bytes.\n"
            "# TYPE node_memory_MemTotal_bytes gauge\n"
            "node_memory_MemTotal_bytes 4.294967296e+09\n"
            "# HELP node_memory_MemAvailable_bytes Memory information field MemAvailable_bytes.\n"
            "# TYPE node_memory_MemAvailable_bytes gauge\n"
            "node_memory_MemAvailable_bytes 2.147483648e+09\n"
        )
        result = parse_prometheus_text(text)
        assert len(result) == 8
        cpu_metrics = [m for m in result if m.name == "node_cpu_seconds_total"]
        assert len(cpu_metrics) == 6
        mem_total = [m for m in result if m.name == "node_memory_MemTotal_bytes"]
        assert len(mem_total) == 1
        assert mem_total[0].value == pytest.approx(4294967296.0)

    def test_label_with_escaped_quotes(self):
        """Labels can contain escaped quotes."""
        text = 'metric{label="value\\"quoted\\"end"} 1.0\n'
        result = parse_prometheus_text(text)
        assert len(result) == 1
        assert result[0].labels["label"] == 'value"quoted"end'

    def test_default_timestamp_is_none(self):
        text = "node_load1 0.42\n"
        result = parse_prometheus_text(text)
        assert result[0].timestamp is None
