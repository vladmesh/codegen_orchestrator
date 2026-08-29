"""The scheduler cadence and allocator freshness window are one runtime policy."""

from pathlib import Path

import pytest
import yaml

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


def test_infra_integration_scheduler_uses_the_deployable_freshness_cadence() -> None:
    """The integration stack must boot with the same valid cadence as deployment."""
    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "docker/test/integration/infra.yml").read_text()
    )
    environment = compose["services"]["scheduler"]["environment"]
    interval = next(value for value in environment if value.startswith("HEALTH_CHECK_INTERVAL="))

    assert validate_health_check_interval(int(interval.split("=", 1)[1])) == 60


def test_shipped_compose_scheduler_default_uses_the_same_freshness_policy() -> None:
    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    )
    interval = compose["services"]["scheduler"]["environment"]["HEALTH_CHECK_INTERVAL"]

    assert interval == "${HEALTH_CHECK_INTERVAL:-60}"
    assert (
        validate_health_check_interval(
            int(interval.removeprefix("${HEALTH_CHECK_INTERVAL:-").removesuffix("}"))
        )
        == 60
    )
