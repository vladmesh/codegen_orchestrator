"""PO reminder poller.

Reads due reminders from the po:reminders sorted set and publishes
them to po:input so the PO consumer picks them up.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

import structlog

from shared.queues import PO_INPUT_QUEUE, PO_REMINDERS_KEY

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = structlog.get_logger(__name__)

POLL_INTERVAL_S = 30


async def _poll_once(redis: Redis) -> int:
    """Run one poll cycle: move due reminders into po:input.

    Returns the number of reminders fired.
    """
    now = time.time()
    due: list[str] = await redis.zrangebyscore(PO_REMINDERS_KEY, 0, now)

    fired = 0
    for entry in due:
        try:
            data = json.loads(entry)
        except (json.JSONDecodeError, TypeError):
            logger.warning("reminder_parse_failed", entry=entry)
            await redis.zrem(PO_REMINDERS_KEY, entry)
            continue

        await redis.xadd(
            PO_INPUT_QUEUE,
            {
                "type": data.get("type", "reminder"),
                "user_id": data.get("user_id", "unknown"),
                "text": data.get("text", ""),
                "timestamp": data.get("timestamp", ""),
            },
        )
        await redis.zrem(PO_REMINDERS_KEY, entry)
        fired += 1

        logger.info(
            "reminder_fired",
            user_id=data.get("user_id"),
            text=data.get("text"),
        )

    return fired


async def run_reminder_poller(redis: Redis) -> None:
    """Poll po:reminders every POLL_INTERVAL_S and fire due reminders."""
    logger.info("reminder_poller_started", poll_interval_s=POLL_INTERVAL_S)
    try:
        while True:
            try:
                fired = await _poll_once(redis)
                if fired:
                    logger.debug("reminder_poll_cycle", fired=fired)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("reminder_poll_error")

            await asyncio.sleep(POLL_INTERVAL_S)
    except asyncio.CancelledError:
        logger.info("reminder_poller_shutdown")
