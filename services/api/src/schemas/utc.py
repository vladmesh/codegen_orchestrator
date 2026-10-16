"""UTC reading of stored timestamps."""

from datetime import UTC, datetime


def assume_utc(value: datetime) -> datetime:
    """A naive datetime read as UTC; an aware one unchanged."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
