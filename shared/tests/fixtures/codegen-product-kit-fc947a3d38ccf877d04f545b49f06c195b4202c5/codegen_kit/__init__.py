"""Stable public API for packages installed into a generated product."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from .packages import (
    CORE_VERSION,
    PACKAGE_PROTOCOL_VERSION,
    Package,
)


def package_base(schema: str) -> Any:
    """Create the package's independent declarative base."""

    from .database import package_base as create_base

    return create_base(schema)


@asynccontextmanager
async def package_session(schema: str) -> AsyncIterator[Any]:
    """Open a core-owned, schema-local package transaction lazily."""

    from .database import package_session as open_session

    async with open_session(schema) as session:
        yield session


async def publish_event(
    stream: str,
    payload: Any,
    *,
    event_id: UUID | None = None,
    occurred_at: datetime | None = None,
    schema_version: int = 1,
) -> Any:
    """Publish through the generated product transport without exposing its module."""

    from shared.generated.events import publish_event as generated_publish_event

    return await generated_publish_event(
        stream,
        payload,
        event_id=event_id,
        occurred_at=occurred_at,
        schema_version=schema_version,
    )


__all__ = [
    "CORE_VERSION",
    "PACKAGE_PROTOCOL_VERSION",
    "Package",
    "package_base",
    "package_session",
    "publish_event",
]
