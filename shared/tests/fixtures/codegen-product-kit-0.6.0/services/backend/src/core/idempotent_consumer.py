"""Transactional idempotency for Redis Stream consumers."""

from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from services.backend.src.app.models.event_consumption import EventConsumption


async def consume_once(
    session: AsyncSession,
    consumer_group: str,
    event_id: UUID,
    effect: Callable[[], Awaitable[None]],
) -> bool:
    """Run one database-backed effect once per group and event identity.

    The marker and effect share the caller's transaction. A crash or exception before
    commit rolls both back, allowing Redis to redeliver the event safely.
    """
    try:
        async with session.begin_nested():
            session.add(EventConsumption(consumer_group=consumer_group, event_id=str(event_id)))
            await session.flush()
    except IntegrityError:
        return False

    await effect()
    return True
