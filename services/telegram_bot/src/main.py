"""Telegram Bot - Main entry point.

Refactored to use POSessionManager with Redis Streams.
Supports progress events display during message processing.
"""

import asyncio
import json
import logging
import os
import sys
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

from shared.contracts.events import ProgressEvent

# Add shared to path
sys.path.insert(0, "/app")
from shared.logging_config import setup_logging  # noqa: E402

from .clients.api import api_client  # noqa: E402
from .config import get_settings  # noqa: E402
from .handlers import handle_callback_query  # noqa: E402
from .keyboards import main_menu_keyboard  # noqa: E402
from .middleware import auth_middleware, is_admin  # noqa: E402
from .notifications import ProvisionerNotifier  # noqa: E402
from .session import POSessionManager  # noqa: E402

logger = structlog.get_logger()

# Global session manager (initialized in post_init)
_session_manager: POSessionManager | None = None
_response_listener_task: asyncio.Task | None = None
_provisioner_notifier_task: asyncio.Task | None = None
_redis_client: redis_lib.Redis | None = None


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


async def handle_message(update: Update, context) -> None:
    """Handle incoming messages - send to PO worker via Redis Streams.

    This is the NEW implementation using POSessionManager with progress tracking.
    """
    global _session_manager, _redis_client

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

    # Indicate typing status
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

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

        # Ensure we have a PO worker
        if _session_manager is None:
            raise RuntimeError("Session manager not initialized")

        await _session_manager.get_or_create_worker(user_id)

        # Create callback stream for progress events
        callback_stream = f"progress:po:{user_id}:{uuid.uuid4().hex[:8]}"

        # Send progress message first
        progress_msg = await update.message.reply_text(
            "🚀 Начинаю обработку...",
            parse_mode=ParseMode.MARKDOWN,
        )

        # Send message to worker with callback stream
        request_id = await _session_manager.send_message(
            user_id, text, callback_stream=callback_stream
        )

        # Start background task to track progress events
        # Note: Final response will still come via _listen_for_worker_responses
        if _redis_client:
            asyncio.create_task(
                _track_progress_with_cleanup(
                    redis=_redis_client,
                    callback_stream=callback_stream,
                    request_id=request_id,
                    bot=context.bot,
                    chat_id=chat_id,
                    progress_message_id=progress_msg.message_id,
                )
            )

    except Exception as e:
        logger.error("message_handling_failed", error=str(e), user_id=user_id)
        await update.message.reply_text(f"⚠️ Ошибка: {e!s}")


async def _track_progress_with_cleanup(
    redis: redis_lib.Redis,
    callback_stream: str,
    request_id: str,
    bot,
    chat_id: int,
    progress_message_id: int,
) -> None:
    """Track progress events and cleanup callback stream after completion."""
    try:
        final_event = await _listen_progress_events(
            redis=redis,
            callback_stream=callback_stream,
            request_id=request_id,
            bot=bot,
            chat_id=chat_id,
            progress_message_id=progress_message_id,
            timeout=300.0,  # 5 minutes max
        )

        if final_event is None:
            # Timeout - update progress message
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_message_id,
                    text="⏰ Таймаут ожидания. Ответ может прийти позже.",
                )
            except Exception as e:
                logger.debug("timeout_message_edit_failed", error=str(e))

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("progress_tracking_error", error=str(e), request_id=request_id)
    finally:
        # Cleanup: delete callback stream (best effort)
        try:
            await redis.delete(callback_stream)
        except Exception as e:
            logger.debug("callback_stream_cleanup_failed", error=str(e))


async def _get_active_session_streams() -> tuple[dict[str, str], dict[str, int]]:
    """Get all active worker output streams and their user mappings."""
    global _redis_client

    streams: dict[str, str] = {}
    user_by_stream: dict[str, int] = {}

    cursor = 0
    session_keys: list[str] = []
    while True:
        cursor, keys = await _redis_client.scan(
            cursor=cursor,
            match="session:po:*",
            count=100,  # noqa: PLR2004
        )
        session_keys.extend(keys)
        if cursor == 0:
            break

    for session_key in session_keys:
        worker_id = await _redis_client.get(session_key)
        if worker_id:
            stream_key = f"worker:po:{worker_id}:output"
            streams[stream_key] = "$"
            user_id_str = session_key.replace("session:po:", "")
            user_by_stream[stream_key] = int(user_id_str)

    return streams, user_by_stream


async def _send_response_to_user(app: Application, user_id: int, text: str) -> None:
    """Send response text to Telegram user with markdown fallback."""
    try:
        await app.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await app.bot.send_message(chat_id=user_id, text=text)

    logger.info("worker_response_sent", user_id=user_id, text_length=len(text))


def _format_progress_message(event: ProgressEvent) -> str:
    """Format progress event for Telegram display."""
    emoji_map = {
        "started": "🚀",
        "progress": "⏳",
        "completed": "✅",
        "failed": "❌",
    }
    emoji = emoji_map.get(event.type, "📌")

    if event.type == "started":
        return f"{emoji} Начинаю обработку..."

    if event.type == "progress":
        parts = [emoji]
        if event.current_step:
            parts.append(f"**{event.current_step}**")
        if event.message:
            parts.append(event.message)
        if event.progress_pct is not None:
            parts.append(f"({event.progress_pct}%)")
        return " ".join(parts)

    if event.type == "completed":
        msg = event.message or "Готово!"
        return f"{emoji} {msg}"

    if event.type == "failed":
        error = event.error or "Неизвестная ошибка"
        return f"{emoji} Ошибка: {error}"

    return f"{emoji} {event.message or ''}"


async def _update_progress_message(
    bot,
    chat_id: int,
    message_id: int,
    event: ProgressEvent,
) -> None:
    """Update Telegram message with progress event."""
    text = _format_progress_message(event)
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        # Message might be the same, or other edit error
        logger.debug("progress_message_edit_failed", error=str(e))


async def _listen_progress_events(
    redis: redis_lib.Redis,
    callback_stream: str,
    request_id: str,
    bot,
    chat_id: int,
    progress_message_id: int,
    timeout: float = 300.0,
) -> ProgressEvent | None:
    """Listen for progress events and update Telegram message.

    Args:
        redis: Redis client
        callback_stream: Stream to listen for events
        request_id: Request ID to filter events
        bot: Telegram bot instance
        chat_id: Chat to update
        progress_message_id: Message ID to edit with progress
        timeout: Max wait time in seconds

    Returns:
        Final event (completed/failed) or None on timeout
    """
    import time

    start_time = time.monotonic()
    last_id = "0"  # Read all messages from start

    logger.debug(
        "listening_progress_events",
        callback_stream=callback_stream,
        request_id=request_id,
        timeout=timeout,
    )

    while True:
        elapsed = time.monotonic() - start_time
        remaining = timeout - elapsed

        if remaining <= 0:
            logger.warning("progress_listen_timeout", request_id=request_id)
            return None

        block_ms = min(2000, int(remaining * 1000))

        try:
            messages = await redis.xread(
                {callback_stream: last_id},
                count=10,
                block=block_ms,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("progress_xread_error", error=str(e))
            await asyncio.sleep(0.5)
            continue

        if not messages:
            continue

        for _, stream_messages in messages:
            for msg_id, msg_data in stream_messages:
                last_id = msg_id

                try:
                    payload = json.loads(msg_data.get("data", "{}"))
                    event = ProgressEvent.model_validate(payload)
                except (json.JSONDecodeError, Exception) as e:
                    logger.debug("invalid_progress_event", error=str(e))
                    continue

                # Filter by request_id
                if event.request_id != request_id:
                    continue

                logger.info(
                    "progress_event_received",
                    event_type=event.type,
                    request_id=request_id,
                    message=event.message,
                )

                # Update progress message
                await _update_progress_message(bot, chat_id, progress_message_id, event)

                # Check for terminal events
                if event.type in ("completed", "failed"):
                    return event

    return None


async def _process_worker_message(app: Application, msg_data: dict, user_id: int) -> None:
    """Process a single worker output message."""
    import json

    payload = json.loads(msg_data.get("data", "{}"))
    response_text = payload.get("content") or payload.get("response", "")

    if response_text:
        await _send_response_to_user(app, user_id, response_text)


async def _listen_for_worker_responses(app: Application) -> None:
    """Listen for PO worker responses and relay to Telegram users."""
    global _redis_client, _session_manager

    if _redis_client is None or _session_manager is None:
        logger.error("Cannot start response listener: Redis/SessionManager not initialized")
        return

    logger.info("worker_response_listener_started")

    try:
        while True:
            try:
                streams, user_by_stream = await _get_active_session_streams()

                if not streams:
                    await asyncio.sleep(1)
                    continue

                messages = await _redis_client.xread(streams, count=10, block=1000)  # noqa: PLR2004

                if not messages:
                    continue

                for stream_name, stream_messages in messages:
                    user_id = user_by_stream.get(stream_name)
                    if not user_id:
                        continue

                    for msg_id, msg_data in stream_messages:
                        try:
                            await _process_worker_message(app, msg_data, user_id)
                        except Exception as e:
                            logger.error(
                                "worker_response_processing_error",
                                error=str(e),
                                msg_id=msg_id,
                            )

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("response_listener_error", error=str(e))
                await asyncio.sleep(1)

    except asyncio.CancelledError:
        logger.info("worker_response_listener_stopped")


async def post_init(app: Application) -> None:
    """Post-initialization: connect clients and start listeners."""
    global _session_manager, _response_listener_task, _provisioner_notifier_task, _redis_client

    settings = get_settings()
    _redis_client = redis_lib.from_url(settings.redis_url, decode_responses=True)
    _session_manager = POSessionManager(redis=_redis_client)

    # Start worker response listener
    _response_listener_task = asyncio.create_task(_listen_for_worker_responses(app))

    # Start provisioner notifications listener
    admin_ids = settings.get_admin_ids()
    notifier = ProvisionerNotifier(redis=_redis_client, admin_ids=admin_ids)
    _provisioner_notifier_task = await notifier.start(app.bot)

    logger.info("telegram_bot_initialized", admin_count=len(admin_ids))


async def post_shutdown(app: Application) -> None:
    """Cleanup on shutdown."""
    global _response_listener_task, _provisioner_notifier_task, _redis_client

    # Stop response listener
    if _response_listener_task:
        _response_listener_task.cancel()
        try:
            await _response_listener_task
        except asyncio.CancelledError:
            pass
        _response_listener_task = None

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
