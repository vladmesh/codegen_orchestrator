"""Generic Prometheus text exposition format parser.

Parses the standard Prometheus /metrics text format into structured Python objects.
Ref: https://prometheus.io/docs/instrumenting/exposition_formats/
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import re

# Regex for a metric line: name{labels} value [timestamp]
# Labels are optional, timestamp is optional.
_METRIC_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>[^}]*)\})?"
    r"\s+(?P<value>\S+)"
    r"(?:\s+(?P<timestamp>\S+))?$"
)

# Regex for individual label pairs inside {}: key="value"
_LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


@dataclass(frozen=True, slots=True)
class PrometheusMetric:
    """A single parsed Prometheus metric sample."""

    name: str
    labels: dict[str, str] = field(default_factory=dict)
    value: float = 0.0
    timestamp: float | None = None


def _parse_labels(raw: str) -> dict[str, str]:
    """Parse label string like 'cpu="0",mode="idle"' into dict."""
    return {k: v.replace('\\"', '"') for k, v in _LABEL_RE.findall(raw)}


def _parse_value(raw: str) -> float:
    """Parse a Prometheus value string (handles +Inf, -Inf, NaN)."""
    if raw == "+Inf":
        return math.inf
    if raw == "-Inf":
        return -math.inf
    if raw == "NaN":
        return math.nan
    return float(raw)


def parse_prometheus_text(text: str) -> list[PrometheusMetric]:
    """Parse Prometheus text exposition format into a list of metrics.

    Skips comment lines (# HELP, # TYPE) and blank lines.
    """
    results: list[PrometheusMetric] = []

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        match = _METRIC_RE.match(line)
        if not match:
            continue

        name = match.group("name")
        labels = _parse_labels(match.group("labels") or "")
        value = _parse_value(match.group("value"))
        ts_raw = match.group("timestamp")
        timestamp = float(ts_raw) if ts_raw else None

        results.append(
            PrometheusMetric(
                name=name,
                labels=labels,
                value=value,
                timestamp=timestamp,
            )
        )

    return results
