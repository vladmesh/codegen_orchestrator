"""Timestamp types for API responses."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator


def _assume_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_assume_utc)]
"""A datetime that marks a naive value as UTC and leaves an aware value unchanged."""
