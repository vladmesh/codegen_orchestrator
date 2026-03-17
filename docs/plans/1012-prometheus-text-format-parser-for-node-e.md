# #1012 Prometheus text format parser for node_exporter + cadvisor metrics

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Phase 1 of Server Health Monitoring (brainstorm bs-69482380). #1011 (provisioning node_exporter + cadvisor) is done — servers expose `:9100/metrics` and `:8080/metrics`. This task creates a pure parser module that converts Prometheus text format into structured Python dicts. No HTTP, no DB — just text in, dict out. Consumed by #1014 (health_checker worker).

Currently: no Prometheus-related code exists. Parser will live in `services/scheduler/src/metrics/`.

## Steps

1. [ ] Generic Prometheus text format parser
   - **Input**: `services/scheduler/src/metrics/__init__.py`, `services/scheduler/src/metrics/parser.py`
   - **Output**: `parse_prometheus_text(text: str) -> list[PrometheusMetric]` — parses standard Prometheus exposition format into a list of dataclass/NamedTuple with fields: `name`, `labels` (dict), `value` (float), `timestamp` (float|None). Skips HELP/TYPE comments, handles missing timestamps, handles multi-line (histogram/summary) and empty input.
   - **Test**: `services/scheduler/tests/unit/test_metrics_parser.py` — parse real-looking node_exporter snippet (counters, gauges, labels, no-label, comments, empty lines). Assert correct metric count, label extraction, value parsing.

2. [ ] Node exporter metric extractor
   - **Input**: `services/scheduler/src/metrics/node_exporter.py`
   - **Output**: `extract_node_metrics(metrics: list[PrometheusMetric]) -> NodeMetrics` dataclass with fields: `cpu_usage_pct` (float), `ram_used_bytes` (int), `ram_total_bytes` (int), `disk_used_bytes` (int), `disk_total_bytes` (int), `load_avg_1m/5m/15m` (float), `uptime_seconds` (float), `network_rx_errors` (int), `network_tx_errors` (int). CPU calculated from `node_cpu_seconds_total` idle ratio. RAM from `node_memory_*`. Disk from `node_filesystem_*` (root mountpoint). Network from `node_network_*`.
   - **Test**: `services/scheduler/tests/unit/test_node_exporter_metrics.py` — feed crafted PrometheusMetric lists, assert correct CPU% calculation, RAM/disk math, graceful handling of missing metrics (return None fields).

3. [ ] Cadvisor metric extractor
   - **Input**: `services/scheduler/src/metrics/cadvisor.py`
   - **Output**: `extract_container_metrics(metrics: list[PrometheusMetric]) -> list[ContainerMetrics]` dataclass per container with: `name` (str), `cpu_usage_seconds` (float), `memory_usage_bytes` (int), `memory_limit_bytes` (int), `network_rx_bytes` (int), `network_tx_bytes` (int), `status` (str). Filter out POD/system containers. Group by container name label.
   - **Test**: `services/scheduler/tests/unit/test_cadvisor_metrics.py` — multi-container input, assert grouping, filtering of infra containers, correct field extraction.

4. [ ] Integration test with realistic metric snapshots
   - **Input**: `services/scheduler/tests/unit/test_metrics_integration.py`, fixture files `services/scheduler/tests/fixtures/node_exporter_sample.txt`, `services/scheduler/tests/fixtures/cadvisor_sample.txt`
   - **Output**: End-to-end test: raw text → parse → extract → assert structured output. Use real (anonymized) metric snapshots from actual node_exporter/cadvisor output to catch edge cases (metric name changes between versions, unusual label formats).
   - **Test**: Parse fixture → extract → assert all expected fields populated, no exceptions on real data.

5. [ ] Public API: convenience function + __init__ exports
   - **Input**: `services/scheduler/src/metrics/__init__.py`
   - **Output**: Clean public API: `from src.metrics import parse_node_exporter, parse_cadvisor` — each takes raw `str`, returns the respective dataclass. These are thin wrappers: `parse_prometheus_text` → `extract_*`. Export dataclasses too.
   - **Test**: Import test in existing integration test — verify public API works end-to-end.

