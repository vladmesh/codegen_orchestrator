"""Extract structured server metrics from node_exporter Prometheus output."""

from __future__ import annotations

from dataclasses import dataclass
import time

from src.metrics.parser import PrometheusMetric


@dataclass(slots=True)
class NodeMetrics:
    """Structured server metrics extracted from node_exporter."""

    cpu_usage_pct: float | None = None
    ram_used_bytes: int | None = None
    ram_total_bytes: int | None = None
    disk_used_bytes: int | None = None
    disk_total_bytes: int | None = None
    load_avg_1m: float | None = None
    load_avg_5m: float | None = None
    load_avg_15m: float | None = None
    uptime_seconds: float | None = None
    network_rx_errors: int | None = None
    network_tx_errors: int | None = None


def _extract_cpu_usage(metrics: list[PrometheusMetric]) -> float | None:
    """Calculate CPU usage % from node_cpu_seconds_total counters.

    CPU usage = 100% - average idle% across all CPUs.
    """
    cpu_metrics = [m for m in metrics if m.name == "node_cpu_seconds_total"]
    if not cpu_metrics:
        return None

    # Group by CPU
    cpus: dict[str, dict[str, float]] = {}
    for m in cpu_metrics:
        cpu_id = m.labels.get("cpu", "0")
        mode = m.labels.get("mode", "")
        cpus.setdefault(cpu_id, {})[mode] = m.value

    # Calculate idle% per CPU, then average
    idle_pcts: list[float] = []
    for modes in cpus.values():
        total = sum(modes.values())
        if total > 0:
            idle_pcts.append(modes.get("idle", 0.0) / total * 100.0)

    if not idle_pcts:
        return None

    avg_idle = sum(idle_pcts) / len(idle_pcts)
    return 100.0 - avg_idle


def _first_value(metrics: list[PrometheusMetric], name: str) -> float | None:
    """Get the value of the first metric matching the given name."""
    for m in metrics:
        if m.name == name:
            return m.value
    return None


def _first_value_with_label(
    metrics: list[PrometheusMetric],
    name: str,
    label_key: str,
    label_value: str,
) -> float | None:
    """Get the value of the first metric matching name and a specific label."""
    for m in metrics:
        if m.name == name and m.labels.get(label_key) == label_value:
            return m.value
    return None


def _sum_values(metrics: list[PrometheusMetric], name: str) -> int | None:
    """Sum all metric values matching the given name."""
    values = [m.value for m in metrics if m.name == name]
    if not values:
        return None
    return int(sum(values))


def extract_node_metrics(metrics: list[PrometheusMetric]) -> NodeMetrics:
    """Extract structured server metrics from a list of parsed node_exporter metrics."""
    result = NodeMetrics()

    # CPU
    result.cpu_usage_pct = _extract_cpu_usage(metrics)

    # RAM
    mem_total = _first_value(metrics, "node_memory_MemTotal_bytes")
    mem_available = _first_value(metrics, "node_memory_MemAvailable_bytes")
    if mem_total is not None:
        result.ram_total_bytes = int(mem_total)
        if mem_available is not None:
            result.ram_used_bytes = int(mem_total - mem_available)

    # Disk (root mountpoint only)
    disk_size = _first_value_with_label(metrics, "node_filesystem_size_bytes", "mountpoint", "/")
    disk_avail = _first_value_with_label(metrics, "node_filesystem_avail_bytes", "mountpoint", "/")
    if disk_size is not None:
        result.disk_total_bytes = int(disk_size)
        if disk_avail is not None:
            result.disk_used_bytes = int(disk_size - disk_avail)

    # Load averages
    result.load_avg_1m = _first_value(metrics, "node_load1")
    result.load_avg_5m = _first_value(metrics, "node_load5")
    result.load_avg_15m = _first_value(metrics, "node_load15")

    # Uptime (from boot time)
    boot_time = _first_value(metrics, "node_boot_time_seconds")
    if boot_time is not None:
        result.uptime_seconds = time.time() - boot_time

    # Network errors (sum across all devices)
    result.network_rx_errors = _sum_values(metrics, "node_network_receive_errs_total")
    result.network_tx_errors = _sum_values(metrics, "node_network_transmit_errs_total")

    return result
