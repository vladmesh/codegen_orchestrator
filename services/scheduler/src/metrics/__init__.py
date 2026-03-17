"""Prometheus metrics parsing for server health monitoring.

Public API:
    parse_node_exporter(text) -> NodeMetrics
    parse_cadvisor(text) -> list[ContainerMetrics]
"""

from src.metrics.cadvisor import ContainerMetrics, extract_container_metrics
from src.metrics.node_exporter import NodeMetrics, extract_node_metrics
from src.metrics.parser import parse_prometheus_text

__all__ = [
    "ContainerMetrics",
    "NodeMetrics",
    "parse_cadvisor",
    "parse_node_exporter",
]


def parse_node_exporter(text: str) -> NodeMetrics:
    """Parse raw node_exporter /metrics text into structured NodeMetrics."""
    return extract_node_metrics(parse_prometheus_text(text))


def parse_cadvisor(text: str) -> list[ContainerMetrics]:
    """Parse raw cadvisor /metrics text into per-container metrics."""
    return extract_container_metrics(parse_prometheus_text(text))
