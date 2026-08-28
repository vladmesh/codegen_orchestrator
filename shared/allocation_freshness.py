"""One policy for health observation cadence and allocation freshness."""

DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS = 60
ALLOCATION_METRICS_FRESHNESS_SECONDS = 300
MINIMUM_STAMPS_PER_WINDOW = 3


def effective_allocation_metrics_freshness_seconds(raw: str | None) -> int:
    """Parse the same optional runtime override the allocator receives."""
    if raw is None:
        return ALLOCATION_METRICS_FRESHNESS_SECONDS
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("ALLOCATION_METRICS_FRESHNESS_SECONDS must be a positive integer") from exc
    if value <= 0:
        raise ValueError("ALLOCATION_METRICS_FRESHNESS_SECONDS must be a positive integer")
    return value


def validate_health_check_interval(
    interval_seconds: int,
    *,
    freshness_seconds: int = ALLOCATION_METRICS_FRESHNESS_SECONDS,
) -> int:
    """Return a usable scheduler cadence or fail before stale metrics are deployed.

    Allocation must remain possible through one missed health-check interval and
    normal scheduling jitter.  The scheduler calls this on its effective
    environment value, so an operator override cannot silently invalidate the
    allocator's admission window.
    """
    if isinstance(interval_seconds, bool) or interval_seconds <= 0:
        raise ValueError("HEALTH_CHECK_INTERVAL must be a positive integer")
    if interval_seconds * MINIMUM_STAMPS_PER_WINDOW > freshness_seconds:
        raise ValueError(
            "HEALTH_CHECK_INTERVAL exceeds the allocation freshness policy: "
            f"{interval_seconds}s allows fewer than {MINIMUM_STAMPS_PER_WINDOW} stamps in "
            f"{freshness_seconds}s"
        )
    return interval_seconds
