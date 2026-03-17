"""Extract per-container metrics from cadvisor Prometheus output."""

from __future__ import annotations

from dataclasses import dataclass

from src.metrics.parser import PrometheusMetric

# Metric names we extract from cadvisor
_CPU = "container_cpu_usage_seconds_total"
_MEM_USAGE = "container_memory_usage_bytes"
_MEM_LIMIT = "container_spec_memory_limit_bytes"
_NET_RX = "container_network_receive_bytes_total"
_NET_TX = "container_network_transmit_bytes_total"

_KNOWN_METRICS = {_CPU, _MEM_USAGE, _MEM_LIMIT, _NET_RX, _NET_TX}


@dataclass(slots=True)
class ContainerMetrics:
    """Structured metrics for a single container."""

    name: str
    cpu_usage_seconds: float | None = None
    memory_usage_bytes: int | None = None
    memory_limit_bytes: int | None = None
    network_rx_bytes: int | None = None
    network_tx_bytes: int | None = None


def _is_real_container(metric: PrometheusMetric) -> bool:
    """Filter out POD-level, root, and system slice entries."""
    name = metric.labels.get("name", "")
    cid = metric.labels.get("id", "")
    if not name:
        return False
    if cid == "/" or cid.startswith("/system.slice"):
        return False
    return True


def extract_container_metrics(metrics: list[PrometheusMetric]) -> list[ContainerMetrics]:
    """Extract per-container metrics from parsed cadvisor Prometheus output.

    Groups metrics by container name. Filters out system/POD containers.
    Returns sorted by container name.
    """
    # Group relevant metrics by container name
    containers: dict[str, dict[str, float]] = {}

    for m in metrics:
        if m.name not in _KNOWN_METRICS:
            continue
        if not _is_real_container(m):
            continue

        container_name = m.labels["name"]
        containers.setdefault(container_name, {})[m.name] = m.value

    # Build result
    results: list[ContainerMetrics] = []
    for name in sorted(containers):
        data = containers[name]
        results.append(
            ContainerMetrics(
                name=name,
                cpu_usage_seconds=data.get(_CPU),
                memory_usage_bytes=_int_or_none(data.get(_MEM_USAGE)),
                memory_limit_bytes=_int_or_none(data.get(_MEM_LIMIT)),
                network_rx_bytes=_int_or_none(data.get(_NET_RX)),
                network_tx_bytes=_int_or_none(data.get(_NET_TX)),
            )
        )

    return results


def _int_or_none(value: float | None) -> int | None:
    """Convert float to int, preserving None."""
    return int(value) if value is not None else None
