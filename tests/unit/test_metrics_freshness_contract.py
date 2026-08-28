"""The scheduler cadence and allocator freshness window are one runtime policy."""

import pytest

from shared.allocation_freshness import (
    ALLOCATION_METRICS_FRESHNESS_SECONDS,
    DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
    effective_allocation_metrics_freshness_seconds,
    validate_health_check_interval,
)


def test_default_cadence_leaves_room_for_a_missed_health_check() -> None:
    assert validate_health_check_interval(DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS) == 60
    assert ALLOCATION_METRICS_FRESHNESS_SECONDS == 300


def test_runtime_override_cannot_make_freshness_expire_between_scheduled_checks() -> None:
    assert validate_health_check_interval(100) == 100
    with pytest.raises(ValueError, match="allocation freshness"):
        validate_health_check_interval(101)


def test_effective_runtime_freshness_override_also_constrains_scheduler_cadence() -> None:
    freshness = effective_allocation_metrics_freshness_seconds("180")

    assert validate_health_check_interval(60, freshness_seconds=freshness) == 60
    with pytest.raises(ValueError, match="allocation freshness"):
        validate_health_check_interval(61, freshness_seconds=freshness)
