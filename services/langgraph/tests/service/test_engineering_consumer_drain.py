"""Service proof that an active drain leaves newly read work in the PEL."""

from __future__ import annotations

import asyncio
import secrets
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_drain_after_an_idle_slot_reservation_does_not_start_a_real_redis_entry(real_redis):
    from src.consumers._base import run_queue_worker

    queue = f"engineering:drain-service:{secrets.token_hex(4)}"
    group = "drain-service"
    processed: list[dict] = []
    draining = False

    async def process(job, _redis):
        processed.append(job)
        return {}

    async def is_draining():
        return draining

    try:
        with (
            patch("src.consumers._base.SLOT_WAIT_SECONDS", 0.01),
            patch(
                "src.consumers._base._check_message_staleness",
                new=AsyncMock(return_value=False),
            ),
        ):
            worker = asyncio.create_task(
                run_queue_worker(
                    "drain-service",
                    queue,
                    process,
                    group=group,
                    is_draining=is_draining,
                )
            )
            await asyncio.sleep(0.1)
            draining = True
            message_id = await real_redis.xadd(queue, {"project_id": "drain-project"})
            await asyncio.sleep(0.1)

            pending = await real_redis.xpending_range(queue, group, "-", "+", 10)
            assert any(entry["message_id"] == message_id for entry in pending)
            assert processed == []
    finally:
        worker.cancel()
        await asyncio.wait_for(worker, timeout=2)
        await real_redis.delete(queue)
