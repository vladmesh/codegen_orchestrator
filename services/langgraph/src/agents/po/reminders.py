"""PO reminder poller.

Reads due reminders from the po:reminders sorted set and publishes
them to po:input so the PO consumer picks them up.
"""

from __future__ import annotations

import asyncio
import json
import time

import structlog

from shared.contracts.queues.po import (
    POReminderMessage,
    to_flat_fields,
    unprotect_po_payload,
)
from shared.queues import PO_INPUT_QUEUE, PO_REMINDERS_KEY
from shared.redis import RedisStreamClient
from shared.redis.po import verify_po_storage

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
            data = unprotect_po_payload(PO_REMINDERS_KEY, json.loads(entry))
            reminder = POReminderMessage.model_validate(data)
        except Exception:
            # Retain evidence and future delivery; never log a member or an exception body.
            logger.warning("reminder_parse_failed")
            raise RuntimeError("PO reminder authentication or validation failed") from None
        await client.publish_flat(PO_INPUT_QUEUE, to_flat_fields(reminder))
        await redis.zrem(PO_REMINDERS_KEY, entry)
        fired += 1

        logger.info(
            "reminder_fired",
            telegram_chat_id=data["telegram_chat_id"],
        )

    return fired


async def run_reminder_poller(client: RedisStreamClient) -> None:
    """Poll po:reminders every POLL_INTERVAL_S and fire due reminders."""
    await verify_po_storage(client.redis)
    logger.info("reminder_poller_started", poll_interval_s=POLL_INTERVAL_S)
    try:
        while True:
            try:
                fired = await _poll_once(client)
                if fired:
                    logger.debug("reminder_poll_cycle", fired=fired)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("reminder_poll_error", error_type=type(exc).__name__)

            await asyncio.sleep(POLL_INTERVAL_S)
    except asyncio.CancelledError:
        logger.info("reminder_poller_shutdown")
