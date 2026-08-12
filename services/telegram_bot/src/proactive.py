"""Delivery of PO proactive notifications to a Telegram chat.

``process_proactive_entry`` is the one place that holds the delivery invariant:
a stream entry it returns from was either accepted by Telegram, or its bounded
attempts were used up and admins were alerted. It acks the entry itself, so the
only way to leave one pending is for the process to die mid-delivery — and the
listener claims the pending entries of its previous incarnation on startup
(``claim_pending``), which brings the message back here. The attempt bound
therefore cannot live in memory: it is read from the group's PEL delivery count,
which survives the restart, so a message that keeps killing its consumer is
still bounded.

Kept apart from ``main`` on purpose: this module takes the bot object as an
argument and imports nothing from ``telegram``, so the delivery contract — how
many attempts, what counts as delivered, what happens when it is not — can be
exercised directly.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum

import structlog

from shared.contracts.queues.po import POProactiveMessage, from_flat_fields
from shared.contracts.recipient import (
    alert_legacy_recipient_field,
    has_legacy_recipient_field,
)
from shared.notifications import notify_admins_best_effort
from shared.queues import PO_PROACTIVE_GROUP, PO_PROACTIVE_QUEUE
from shared.redis.client import RedisStreamClient, StreamMessage

logger = structlog.get_logger()

# Attempts inside one delivery, so a chat that is briefly unreachable is retried
# without wedging the listener.
PROACTIVE_MAX_ATTEMPTS = 3
PROACTIVE_RETRY_DELAY_S = 1.0
# Times the group may be handed the same entry before the message is given up on.
# Counted by Redis, so a consumer that dies mid-delivery cannot restart the count.
PROACTIVE_MAX_DELIVERIES = 3
# Pending entries are claimed on startup regardless of how long they have been
# idle: the consumer that held them is this consumer's dead predecessor, and a
# fast restart must not leave a notification unattended.
PROACTIVE_RECLAIM_IDLE_MS = 0


class ProactiveOutcome(StrEnum):
    """How ``process_proactive_entry`` finished with a stream entry."""

    DELIVERED = "delivered"
    EXHAUSTED = "exhausted"
    REJECTED = "rejected"


async def send_message_to_chat(bot, chat_id: int, text: str) -> None:
    """Send text to a Telegram chat, falling back to plain text on markup errors."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except Exception:
        await bot.send_message(chat_id=chat_id, text=text)


async def attempt_proactive_delivery(bot, proactive: POProactiveMessage) -> str | None:
    """Try to deliver one proactive message, retrying within this delivery.

    Returns ``None`` once Telegram accepted it, otherwise the last error. Raising
    an alert is not this function's job: whether a failure is final depends on
    how often the entry has already been delivered, which only
    ``process_proactive_entry`` knows.
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
            return None
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

    return str(last_error)


async def _alert_delivery_exhausted(
    proactive: POProactiveMessage, *, deliveries: int, error: str
) -> None:
    """Report a notification the user will never receive."""
    logger.error(
        "proactive_message_delivery_exhausted",
        error=error,
        telegram_chat_id=proactive.telegram_chat_id,
        attempts=PROACTIVE_MAX_ATTEMPTS,
        deliveries=deliveries,
        max_deliveries=PROACTIVE_MAX_DELIVERIES,
        po_event=proactive.event,
        story_id=proactive.story_id,
        project_id=proactive.project_id,
    )
    await notify_admins_best_effort(
        f"Telegram delivery failed after {deliveries} deliveries "
        f"of {PROACTIVE_MAX_ATTEMPTS} attempts: chat={proactive.telegram_chat_id} "
        f"event={proactive.event or '-'} story={proactive.story_id or '-'} "
        f"project={proactive.project_id or '-'} "
        f"owner_user_id={proactive.owner_user_id or '-'} error={error}",
        level="error",
        po_event=proactive.event,
        story_id=proactive.story_id,
        project_id=proactive.project_id,
    )


async def process_proactive_entry(
    bot, client: RedisStreamClient, msg: StreamMessage
) -> ProactiveOutcome:
    """Deliver one po:proactive entry, or give up on it visibly.

    Acks the entry in every outcome, and only after the outcome is settled:
    delivered, refused as unaddressable, or out of attempts with an admin alert
    naming the story, project and event. The three outcomes are distinct in the
    logs (``proactive_message_sent``, ``proactive_message_invalid``,
    ``proactive_message_delivery_exhausted``).
    """
    deliveries = await client.delivery_count(PO_PROACTIVE_QUEUE, PO_PROACTIVE_GROUP, msg.message_id)

    try:
        proactive = from_flat_fields(msg.data, POProactiveMessage)
    except Exception as e:
        logger.error(
            "proactive_message_invalid",
            error=str(e),
            entry_id=msg.message_id,
            telegram_chat_id=msg.data.get("telegram_chat_id"),
        )
        if has_legacy_recipient_field(msg.data):
            await alert_legacy_recipient_field(
                source=PO_PROACTIVE_QUEUE, entry_id=msg.message_id, data=msg.data
            )
        await client.ack(PO_PROACTIVE_QUEUE, PO_PROACTIVE_GROUP, msg.message_id)
        return ProactiveOutcome.REJECTED

    if deliveries > PROACTIVE_MAX_DELIVERIES:
        # Handed out more times than the bound allows: every previous delivery
        # ended before this function could settle it, so retrying again would
        # only repeat whatever kills it.
        await _alert_delivery_exhausted(
            proactive,
            deliveries=deliveries,
            error=f"entry redelivered {deliveries} times without completing",
        )
        await client.ack(PO_PROACTIVE_QUEUE, PO_PROACTIVE_GROUP, msg.message_id)
        return ProactiveOutcome.EXHAUSTED

    error = await attempt_proactive_delivery(bot, proactive)
    if error is None:
        await client.ack(PO_PROACTIVE_QUEUE, PO_PROACTIVE_GROUP, msg.message_id)
        return ProactiveOutcome.DELIVERED

    await _alert_delivery_exhausted(proactive, deliveries=deliveries, error=error)
    await client.ack(PO_PROACTIVE_QUEUE, PO_PROACTIVE_GROUP, msg.message_id)
    return ProactiveOutcome.EXHAUSTED
