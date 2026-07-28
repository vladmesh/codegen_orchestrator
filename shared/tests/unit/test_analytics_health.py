"""Unit tests for the analytics collection health classifier."""

import datetime as dt

from shared.analytics_health import (
    ANALYTICS_HEARTBEAT_MAX_AGE,
    CollectionState,
    Heartbeat,
    collection_state,
    decode_heartbeat,
    encode_heartbeat,
)

NOW = dt.datetime(2026, 7, 28, 12, 0, tzinfo=dt.UTC)
PROJECT = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def _beat(age: dt.timedelta = dt.timedelta(minutes=55), failed=frozenset()):
    return Heartbeat(completed_at=NOW - age, failed_project_ids=frozenset(failed))


def test_no_heartbeat_means_never_collected():
    assert collection_state(None, NOW) is CollectionState.NEVER


def test_recent_clean_heartbeat_is_ok():
    assert collection_state(_beat(), NOW, PROJECT) is CollectionState.OK


def test_heartbeat_at_the_age_limit_is_still_ok():
    assert collection_state(_beat(ANALYTICS_HEARTBEAT_MAX_AGE), NOW) is CollectionState.OK


def test_older_heartbeat_is_stale():
    older = ANALYTICS_HEARTBEAT_MAX_AGE + dt.timedelta(minutes=1)
    assert collection_state(_beat(older), NOW) is CollectionState.STALE


def test_failed_project_is_failing_not_empty():
    """A finished cycle that collected nothing for a project is not 'no traffic'."""
    assert collection_state(_beat(failed={PROJECT}), NOW, PROJECT) is CollectionState.FAILING


def test_other_projects_stay_ok_when_one_fails():
    assert collection_state(_beat(failed={OTHER}), NOW, PROJECT) is CollectionState.OK


def test_pipeline_view_reports_failing_when_any_project_failed():
    assert collection_state(_beat(failed={OTHER}), NOW) is CollectionState.FAILING


def test_heartbeat_roundtrip():
    decoded = decode_heartbeat(encode_heartbeat(NOW, {OTHER, PROJECT}))

    assert decoded.completed_at == NOW
    assert decoded.failed_project_ids == frozenset({PROJECT, OTHER})
