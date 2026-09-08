"""Tests for durable event-consumer idempotency."""

from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from services.backend.src.app.models.event_consumption import EventConsumption
from services.backend.src.core.idempotent_consumer import consume_once


@pytest.mark.asyncio
async def test_duplicate_event_runs_effect_once(db_session: AsyncSession) -> None:
    event_id = uuid4()
    calls = 0

    async def effect() -> None:
        nonlocal calls
        calls += 1

    first = await consume_once(db_session, "events:backend", event_id, effect)
    await db_session.commit()
    second = await consume_once(db_session, "events:backend", event_id, effect)
    await db_session.commit()

    count = await db_session.scalar(select(func.count()).select_from(EventConsumption))
    assert first is True
    assert second is False
    assert calls == 1
    assert count == 1
