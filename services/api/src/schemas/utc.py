"""Timestamp types for API responses."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator


def assume_utc(value: datetime) -> datetime:
    """A naive datetime read as UTC; an aware one unchanged."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


UtcDatetime = Annotated[datetime, AfterValidator(assume_utc)]
"""A datetime validated by `assume_utc`; a naive stored value serialises with a UTC designator."""
