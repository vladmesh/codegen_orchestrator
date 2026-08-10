"""Delivery of PO proactive notifications to a Telegram chat.

Kept apart from ``main`` on purpose: this module takes the bot object as an
argument and imports nothing from ``telegram``, so the delivery contract — how
many attempts, what counts as delivered, what happens when it is not — can be
exercised directly.
"""

from __future__ import annotations

import asyncio

import structlog

from shared.contracts.queues.po import POProactiveMessage
from shared.notifications import notify_admins_best_effort

logger = structlog.get_logger()

# Bounded so a chat that keeps refusing the message cannot wedge the listener,
# and the exhaustion is reported rather than swallowed.
PROACTIVE_MAX_ATTEMPTS = 3
PROACTIVE_RETRY_DELAY_S = 1.0


async def send_message_to_chat(bot, chat_id: int, text: str) -> None:
    """Send text to a Telegram chat, falling back to plain text on markup errors."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except Exception:
        await bot.send_message(chat_id=chat_id, text=text)


async def deliver_proactive_message(bot, proactive: POProactiveMessage) -> bool:
    """Deliver one proactive message, retrying a bounded number of times.

    Returns True once Telegram accepted it. On exhaustion it returns False after
    an admin alert naming the story, project and event — a message the user never
    received is a visible failure, not a log line nobody reads.
    """
    chat_id = int(proactive.telegram_chat_id)
    last_error: Exception | None = None

    for attempt in range(1, PROACTIVE_MAX_ATTEMPTS + 1):
        try:
            await send_message_to_chat(bot, chat_id, proactive.text)
            logger.info(
                "proactive_message_sent",
                telegram_chat_id=chat_id,
                attempts=attempt,
                text_length=len(proactive.text),
            )
            return True
        except Exception as e:
            last_error = e
            logger.warning(
                "proactive_message_send_failed",
                error=str(e),
                telegram_chat_id=chat_id,
                attempt=attempt,
                max_attempts=PROACTIVE_MAX_ATTEMPTS,
            )
            if attempt < PROACTIVE_MAX_ATTEMPTS:
                await asyncio.sleep(PROACTIVE_RETRY_DELAY_S * attempt)

    logger.error(
        "proactive_message_delivery_exhausted",
        error=str(last_error),
        telegram_chat_id=chat_id,
        attempts=PROACTIVE_MAX_ATTEMPTS,
        po_event=proactive.event,
        story_id=proactive.story_id,
        project_id=proactive.project_id,
    )
    await notify_admins_best_effort(
        f"Telegram delivery failed after {PROACTIVE_MAX_ATTEMPTS} attempts: "
        f"chat={chat_id} event={proactive.event or '-'} "
        f"story={proactive.story_id or '-'} project={proactive.project_id or '-'} "
        f"owner_user_id={proactive.owner_user_id or '-'} error={last_error}",
        level="error",
        po_event=proactive.event,
        story_id=proactive.story_id,
        project_id=proactive.project_id,
    )
    return False
