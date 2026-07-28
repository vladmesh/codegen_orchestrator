"""Unit tests for the analytics collection health classifier."""

import datetime as dt

from shared.analytics_health import (
    ANALYTICS_HEARTBEAT_MAX_AGE,
    CollectionState,
    collection_state,
)

NOW = dt.datetime(2026, 7, 28, 12, 0, tzinfo=dt.UTC)


def test_no_heartbeat_means_never_collected():
    assert collection_state(None, NOW) is CollectionState.NEVER


def test_recent_heartbeat_is_ok():
    assert collection_state(NOW - dt.timedelta(minutes=55), NOW) is CollectionState.OK


def test_heartbeat_at_the_age_limit_is_still_ok():
    assert collection_state(NOW - ANALYTICS_HEARTBEAT_MAX_AGE, NOW) is CollectionState.OK


def test_older_heartbeat_is_stale():
    older = NOW - ANALYTICS_HEARTBEAT_MAX_AGE - dt.timedelta(minutes=1)
    assert collection_state(older, NOW) is CollectionState.STALE
