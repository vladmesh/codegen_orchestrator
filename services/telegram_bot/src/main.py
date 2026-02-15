"""Telegram Bot - Main entry point.

Direct PO ReactAgent communication via Redis Streams.
Messages flow: XADD po:input → PO consumer → XREAD po:response:{request_id}.
"""

import asyncio
import logging
import os
import sys
import time
import uuid

import httpx
import redis.asyncio as redis_lib
import structlog
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from shared.queues import PO_INPUT_QUEUE

# Add shared to path
sys.path.insert(0, "/app")
from shared.log_config import setup_logging  # noqa: E402

from .clients.api import api_client  # noqa: E402
from .config import get_settings  # noqa: E402
from .handlers import handle_callback_query  # noqa: E402
from .keyboards import main_menu_keyboard  # noqa: E402
from .middleware import auth_middleware, is_admin  # noqa: E402
from .notifications import ProvisionerNotifier  # noqa: E402

logger = structlog.get_logger()

# Globals (initialized in post_init)
_provisioner_notifier_task: asyncio.Task | None = None
_redis_client: redis_lib.Redis | None = None

# PO response settings
PO_RESPONSE_TIMEOUT_S = 60
TYPING_INTERVAL_S = 5


async def _post_rag_message(payload: dict) -> None:
    """Log message to RAG system (fire and forget)."""
    headers = {}
    if payload.get("telegram_id"):
        headers["X-Telegram-ID"] = str(payload["telegram_id"])

    try:
        await api_client.post_json("rag/messages", headers=headers, json=payload)
    except httpx.HTTPError as e:
        logger.warning("rag_message_log_failed", error=str(e))


async def start(update: Update, context) -> None:
    """Handle /start command - show main menu."""
    if update.effective_user:
        await _ensure_user_registered(update.effective_user)
    user_is_admin = is_admin(context)
    await update.message.reply_text(
        "🏠 **Главное меню**\n\n"
        "Привет! Я оркестратор для генерации проектов.\n\n"
        "Выберите действие или опишите проект в чате:",
        reply_markup=main_menu_keyboard(is_admin=user_is_admin),
        parse_mode=ParseMode.MARKDOWN,
    )


async def menu(update: Update, context) -> None:
    """Handle /menu command - show main menu."""
    if update.effective_user:
        await _ensure_user_registered(update.effective_user)
    user_is_admin = is_admin(context)
    await update.message.reply_text(
        "🏠 **Главное меню**\n\nВыберите действие:",
        reply_markup=main_menu_keyboard(is_admin=user_is_admin),
        parse_mode=ParseMode.MARKDOWN,
    )


async def _ensure_user_registered(tg_user) -> None:
    """Upsert user in database via API."""
    settings = get_settings()
    admin_ids = settings.get_admin_ids()
    is_admin_user = tg_user.id in admin_ids

    payload = {
        "telegram_id": tg_user.id,
        "username": tg_user.username,
        "first_name": tg_user.first_name,
        "last_name": tg_user.last_name,
        "is_admin": is_admin_user,
    }

    headers = {"X-Telegram-ID": str(tg_user.id)}

    try:
        await api_client.post_json("users/upsert", headers=headers, json=payload)
    except httpx.HTTPError as e:
        logger.warning("user_registration_failed", error=str(e))


async def _keep_typing(bot, chat_id: int, max_duration_s: float = 120.0) -> None:
    """Send typing indicator every TYPING_INTERVAL_S until cancelled.

    Args:
        bot: Telegram bot instance
        chat_id: Chat to show typing in
        max_duration_s: Safety limit to prevent infinite typing
    """
    start_time = time.monotonic()
    try:
        while (time.monotonic() - start_time) < max_duration_s:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
            await asyncio.sleep(TYPING_INTERVAL_S)
    except asyncio.CancelledError:
        pass


async def _read_po_response(
    redis: redis_lib.Redis,
    response_stream: str,
    timeout_s: float,
) -> dict | None:
    """Read PO response from a per-request stream.

    Uses id="0" to read from beginning — catches response even if
    written before XREAD starts (no race condition).

    Args:
        redis: Redis client
        response_stream: Stream name (po:response:{request_id})
        timeout_s: Max wait time in seconds

    Returns:
        Response data dict or None on timeout
    """
    start_time = time.monotonic()

    while True:
        elapsed = time.monotonic() - start_time
        remaining = timeout_s - elapsed

        if remaining <= 0:
            return None

        block_ms = min(2000, int(remaining * 1000))

        try:
            messages = await redis.xread(
                {response_stream: "0"},
                count=1,
                block=block_ms,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("po_response_xread_error", error=str(e))
            await asyncio.sleep(0.5)
            continue

        if not messages:
            continue

        for _stream_name, stream_messages in messages:
            if stream_messages:
                _msg_id, data = stream_messages[0]
                return data

    return None


async def _send_to_po_and_wait(
    redis: redis_lib.Redis,
    user_id: int,
    text: str,
    bot,
    chat_id: int,
) -> str:
    """Send message to PO via po:input and wait for response.

    Orchestrates: XADD → typing task → XREAD → cleanup.

    Args:
        redis: Redis client
        user_id: Telegram user ID
        text: User message text
        bot: Telegram bot instance
        chat_id: Chat ID for typing indicator

    Returns:
        PO response text

    Raises:
        TimeoutError: If no response within PO_RESPONSE_TIMEOUT_S
        RuntimeError: If PO returned an error
    """
    request_id = str(uuid.uuid4())
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    response_stream = f"po:response:{request_id}"

    # Publish to PO input stream
    await redis.xadd(
        PO_INPUT_QUEUE,
        {
            "type": "user_message",
            "text": text,
            "user_id": str(user_id),
            "request_id": request_id,
            "timestamp": timestamp,
        },
    )

    logger.info(
        "po_message_sent",
        user_id=user_id,
        request_id=request_id,
    )

    # Start typing indicator in background
    typing_task = asyncio.create_task(_keep_typing(bot, chat_id))

    try:
        # Wait for response
        data = await _read_po_response(redis, response_stream, PO_RESPONSE_TIMEOUT_S)

        if data is None:
            raise TimeoutError(f"PO did not respond within {PO_RESPONSE_TIMEOUT_S}s")

        # Check for error response
        if data.get("error") == "true":
            error_text = data.get("text", "Unknown error")
            raise RuntimeError(error_text)

        response_text = data.get("text", "")
        if not response_text:
            raise RuntimeError("PO returned empty response")

        return response_text

    finally:
        # Cancel typing indicator
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass

        # Cleanup response stream (best effort)
        try:
            await redis.delete(response_stream)
        except Exception as e:
            logger.debug("response_stream_cleanup_failed", error=str(e))


async def handle_message(update: Update, context) -> None:
    """Handle incoming messages - send to PO ReactAgent via Redis Streams."""
    global _redis_client

    # Auth check
    if not await auth_middleware(update, context):
        return

    # Ensure user is registered in DB
    if update.effective_user:
        await _ensure_user_registered(update.effective_user)

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    text = update.message.text

    logger.info("message_received", user_id=user_id, text_length=len(text) if text else 0)

    try:
        # Log user message to RAG (fire and forget)
        asyncio.create_task(
            _post_rag_message(
                {
                    "telegram_id": user_id,
                    "role": "user",
                    "message_text": text,
                    "message_id": str(message_id),
                    "source": "telegram",
                }
            )
        )

        if _redis_client is None:
            raise RuntimeError("Redis client not initialized")

        # Send to PO and wait for response
        response_text = await _send_to_po_and_wait(
            redis=_redis_client,
            user_id=user_id,
            text=text,
            bot=context.bot,
            chat_id=chat_id,
        )

        # Send response to user
        await _send_response_to_user(context.application, user_id, response_text)

    except TimeoutError:
        logger.warning("po_response_timeout", user_id=user_id)
        await update.message.reply_text("Таймаут ожидания ответа. Попробуйте позже.")
    except RuntimeError as e:
        logger.error("po_response_error", error=str(e), user_id=user_id)
        await update.message.reply_text(f"Ошибка: {e!s}")
    except Exception as e:
        logger.error("message_handling_failed", error=str(e), user_id=user_id)
        await update.message.reply_text(f"Ошибка: {e!s}")


async def _send_response_to_user(app: Application, user_id: int, text: str) -> None:
    """Send response text to Telegram user with markdown fallback."""
    try:
        await app.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await app.bot.send_message(chat_id=user_id, text=text)

    logger.info("worker_response_sent", user_id=user_id, text_length=len(text))


async def post_init(app: Application) -> None:
    """Post-initialization: connect Redis and start listeners."""
    global _provisioner_notifier_task, _redis_client

    settings = get_settings()
    _redis_client = redis_lib.from_url(settings.redis_url, decode_responses=True)

    # Start provisioner notifications listener
    admin_ids = settings.get_admin_ids()
    notifier = ProvisionerNotifier(redis=_redis_client, admin_ids=admin_ids)
    _provisioner_notifier_task = await notifier.start(app.bot)

    logger.info("telegram_bot_initialized", admin_count=len(admin_ids))


async def post_shutdown(app: Application) -> None:
    """Cleanup on shutdown."""
    global _provisioner_notifier_task, _redis_client

    # Stop provisioner notifier
    if _provisioner_notifier_task:
        _provisioner_notifier_task.cancel()
        try:
            await _provisioner_notifier_task
        except asyncio.CancelledError:
            pass
        _provisioner_notifier_task = None

    # Close Redis
    if _redis_client:
        await _redis_client.aclose()
        _redis_client = None

    await api_client.close()


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

    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))

    # Global Auth Middleware (runs first for everything else)
    app.add_handler(MessageHandler(filters.ALL, lambda u, c: auth_middleware(u, c)), group=-1)

    # Callback query handler for inline buttons
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # Text message handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("telegram_bot_starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
