"""The error and warning log lines about one story or its project, read from Loki.

The one place the API reads the log store. It is a bounded, read-only search
for lines that name the story id or the project id, at ``error`` or
``warning`` level, across every compose service (the ``compose_service`` label
Promtail sets). Only named fields are copied out of each structured line and
each is redacted and bounded, so a line never carries anything else its service
logged next to the error.

The log store is observability, not state: when ``LOKI_URL`` is not configured
or the query fails, the caller gets an empty list and the reason, and the rest
of the diagnostics — all of which come from the database — are unaffected.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
import re

import structlog

from shared.clients.loki import LokiClient
from shared.contracts.dto.story_failure import (
    STORY_DIAGNOSTIC_LOG_FIELD_LIMIT,
    StoryDiagnosticLogLine,
    bounded_diagnostic,
)

logger = structlog.get_logger()

#: How long one log read may take before the diagnostics answer without logs.
LOKI_READ_TIMEOUT_SECONDS = 10.0

#: Ids are interpolated into a LogQL regex; anything else is refused.
_SAFE_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def story_log_query(story_id: str, project_id: str) -> str:
    """The LogQL selecting error/warning lines that mention the story or project."""
    for value in (story_id, project_id):
        if not _SAFE_ID.match(value):
            raise ValueError(f"{value!r} is not a safe log search id")
    return f'{{compose_service=~".+"}} |~ "{story_id}|{project_id}" | json | level=~"error|warning"'


def _field(entry: dict, *names: str) -> str | None:
    for name in names:
        value = entry.get(name)
        if value not in (None, ""):
            return bounded_diagnostic(value, STORY_DIAGNOSTIC_LOG_FIELD_LIMIT)
    return None


def to_log_line(entry: dict) -> StoryDiagnosticLogLine:
    labels = entry.get("_labels") or {}
    return StoryDiagnosticLogLine(
        timestamp=_field(entry, "timestamp"),
        service=_field(entry, "service") or _field(labels, "compose_service"),
        level=_field(entry, "level"),
        event=_field(entry, "event"),
        error=_field(entry, "error", "reason", "detail"),
    )


async def read_story_logs(
    story_id: str, project_id: str, *, hours: int, limit: int
) -> tuple[list[StoryDiagnosticLogLine], str | None]:
    """The newest ``limit`` matching lines of the last ``hours``, or why there are none."""
    if not os.environ.get("LOKI_URL"):
        return [], "log store is not configured (LOKI_URL is not set)"
    end = datetime.now(UTC)
    client = LokiClient()
    try:
        entries = await asyncio.wait_for(
            client.query_range(
                story_log_query(story_id, project_id),
                start=end - timedelta(hours=hours),
                end=end,
                limit=limit,
                direction="backward",
            ),
            timeout=LOKI_READ_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "story_diagnostics_logs_unavailable",
            story_id=story_id,
            error_type=type(exc).__name__,
        )
        return [], f"log store could not be read ({type(exc).__name__})"
    finally:
        await client.close()
    return [to_log_line(entry) for entry in entries[:limit]], None
