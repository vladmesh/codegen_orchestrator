"""PO reminder poller.

Reads due reminders from the po:reminders sorted set and publishes
them to po:input so the PO consumer picks them up.
"""

from __future__ import annotations

import asyncio
import json
import time

import structlog

from shared.contracts.queues.po import POReminderMessage, to_flat_fields
from shared.queues import PO_INPUT_QUEUE, PO_REMINDERS_KEY
from shared.redis_client import RedisStreamClient

logger = structlog.get_logger(__name__)

POLL_INTERVAL_S = 30


async def _poll_once(client: RedisStreamClient) -> int:
    """Run one poll cycle: move due reminders into po:input.

    Returns the number of reminders fired.
    """
    redis = client.redis
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

        reminder = POReminderMessage(
            text=data.get("text", ""),
            user_id=data.get("user_id", "unknown"),
            timestamp=data.get("timestamp", ""),
        )
        await client.publish_flat(PO_INPUT_QUEUE, to_flat_fields(reminder))
        await redis.zrem(PO_REMINDERS_KEY, entry)
        fired += 1

        logger.info(
            "reminder_fired",
            user_id=data.get("user_id"),
            text=data.get("text"),
        )

    return fired


async def run_reminder_poller(client: RedisStreamClient) -> None:
    """Poll po:reminders every POLL_INTERVAL_S and fire due reminders."""
    logger.info("reminder_poller_started", poll_interval_s=POLL_INTERVAL_S)
    try:
        while True:
            try:
                fired = await _poll_once(client)
                if fired:
                    logger.debug("reminder_poll_cycle", fired=fired)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("reminder_poll_error")

            await asyncio.sleep(POLL_INTERVAL_S)
    except asyncio.CancelledError:
        logger.info("reminder_poller_shutdown")
