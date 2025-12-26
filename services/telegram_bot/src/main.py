"""Telegram Bot - Main entry point."""

import asyncio
import logging
import os
import sys
import time

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
import structlog

# Add shared to path
sys.path.insert(0, "/app")
from shared.logging_config import setup_logging
from shared.redis_client import RedisStreamClient

logger = structlog.get_logger()

# Global Redis client
redis_client = RedisStreamClient()


async def start(update: Update, context) -> None:
    """Handle /start command."""
    await update.message.reply_text(
        "Привет! Я оркестратор для генерации проектов.\nОпиши, какой проект ты хочешь создать."
    )


async def handle_message(update: Update, context) -> None:
    """Handle incoming messages - publish to Redis Stream."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    text = update.message.text
    correlation_id = f"msg_{message_id}_{int(time.time())}"

    preexisting_context = structlog.contextvars.get_contextvars()
    bind_keys = []
    for key, value in {
        "correlation_id": correlation_id,
        "user_id": user_id,
        "chat_id": chat_id,
        "message_id": message_id,
    }.items():
        if key not in preexisting_context:
            bind_keys.append(key)
        structlog.contextvars.bind_contextvars(**{key: value})

    logger.info(
        "message_received",
        text_length=len(text) if text else 0,
    )

    try:
        # Publish to Redis Stream for LangGraph to process
        await redis_client.publish(
            RedisStreamClient.INCOMING_STREAM,
            {
                "user_id": user_id,
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "thread_id": f"user_{user_id}",  # For LangGraph checkpointing
                "correlation_id": correlation_id,
            },
        )

        logger.info("message_published", stream=RedisStreamClient.INCOMING_STREAM)
    finally:
        if bind_keys:
            structlog.contextvars.unbind_contextvars(*bind_keys)


async def outgoing_consumer(bot: Bot) -> None:
    """Consume outgoing messages from Redis and send to Telegram."""
    await redis_client.connect()

    logger.info("Starting outgoing message consumer...")

    async for message in redis_client.consume(
        stream=RedisStreamClient.OUTGOING_STREAM,
        group="telegram_bot",
        consumer="bot_sender",
    ):
        data = message.data
        chat_id = data.get("chat_id")
        text = data.get("text", "")
        reply_to = data.get("reply_to_message_id")

        if not chat_id or not text:
            logger.warning("invalid_outgoing_message", payload=data)
            continue

        try:
            correlation_id = data.get("correlation_id")
            preexisting_context = structlog.contextvars.get_contextvars()
            bind_keys = []
            if correlation_id and "correlation_id" not in preexisting_context:
                bind_keys.append("correlation_id")
            if correlation_id:
                structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

            logger.info("sending_message", chat_id=chat_id, reply_to_message_id=reply_to)
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=reply_to,
            )
            logger.info("message_sent", chat_id=chat_id)
        except Exception as e:
            logger.error(
                "message_send_failed",
                chat_id=chat_id,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
        finally:
            if bind_keys:
                structlog.contextvars.unbind_contextvars(*bind_keys)


async def post_init(app: Application) -> None:
    """Post-initialization: connect to Redis and start consumer."""
    await redis_client.connect()

    # Start outgoing message consumer as background task
    asyncio.create_task(outgoing_consumer(app.bot))
    logger.info("Telegram bot initialized with Redis consumer")


async def post_shutdown(app: Application) -> None:
    """Cleanup on shutdown."""
    await redis_client.close()


def main() -> None:
    """Run the bot."""
    setup_logging(service_name="telegram_bot")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    app = (
        Application.builder().token(token).post_init(post_init).post_shutdown(post_shutdown).build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("telegram_bot_starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
