"""Shared contract for the analytics collection heartbeat.

The scheduler's analytics aggregator publishes what each cycle actually
achieved: when it finished and which projects it failed to collect. The LK API
reads it to tell "this project has no traffic" apart from "nothing was
collected for this project".
"""

from dataclasses import dataclass
import datetime as dt
from enum import StrEnum
import json

# system_configs key holding the JSON heartbeat of the last completed cycle
ANALYTICS_HEARTBEAT_KEY = "analytics.aggregator_last_success_at"

# Cycles run hourly at :05, so anything older than two hours means the
# aggregator is not running or dies before it finishes a cycle.
ANALYTICS_HEARTBEAT_MAX_AGE = dt.timedelta(hours=2)


class CollectionState(StrEnum):
    """State of the analytics collection pipeline, as reported to the LK."""

    OK = "ok"
    FAILING = "failing"
    STALE = "stale"
    NEVER = "never"


@dataclass(frozen=True)
class Heartbeat:
    """Result of the last aggregation cycle that ran to completion."""

    completed_at: dt.datetime
    failed_project_ids: frozenset[str]


def encode_heartbeat(completed_at: dt.datetime, failed_project_ids: set[str]) -> str:
    """Serialize a cycle result for the system_configs value column."""
    return json.dumps(
        {
            "completed_at": completed_at.isoformat(),
            "failed_project_ids": sorted(failed_project_ids),
        }
    )


def decode_heartbeat(raw: str) -> Heartbeat:
    """Parse a stored heartbeat. Malformed values raise."""
    payload = json.loads(raw)
    return Heartbeat(
        completed_at=dt.datetime.fromisoformat(payload["completed_at"]),
        failed_project_ids=frozenset(payload["failed_project_ids"]),
    )


def collection_state(
    heartbeat: Heartbeat | None,
    now: dt.datetime,
    project_id: str | None = None,
) -> CollectionState:
    """Classify collection health, for one project or for the pipeline overall.

    A cycle that finished but collected nothing for a project is a failure for
    that project, not an empty result.
    """
    if heartbeat is None:
        return CollectionState.NEVER
    if now - heartbeat.completed_at > ANALYTICS_HEARTBEAT_MAX_AGE:
        return CollectionState.STALE
    if project_id is None:
        return CollectionState.FAILING if heartbeat.failed_project_ids else CollectionState.OK
    if project_id in heartbeat.failed_project_ids:
        return CollectionState.FAILING
    return CollectionState.OK
