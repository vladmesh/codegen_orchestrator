"""Shared contract for the analytics collection heartbeat.

The scheduler's analytics aggregator writes a timestamp after every completed
cycle; the LK API reads it to tell "this project has no traffic" apart from
"nothing is collecting analytics at all".
"""

import datetime as dt
from enum import StrEnum

# system_configs key holding the ISO-8601 UTC timestamp of the last completed cycle
ANALYTICS_HEARTBEAT_KEY = "analytics.aggregator_last_success_at"

# Cycles run hourly at :05, so anything older than two hours means the
# aggregator is not running or is failing every cycle.
ANALYTICS_HEARTBEAT_MAX_AGE = dt.timedelta(hours=2)


class CollectionState(StrEnum):
    """State of the analytics collection pipeline, as reported to the LK."""

    OK = "ok"
    STALE = "stale"
    NEVER = "never"


def collection_state(last_success_at: dt.datetime | None, now: dt.datetime) -> CollectionState:
    """Classify collection health from the last heartbeat."""
    if last_success_at is None:
        return CollectionState.NEVER
    if now - last_success_at > ANALYTICS_HEARTBEAT_MAX_AGE:
        return CollectionState.STALE
    return CollectionState.OK
