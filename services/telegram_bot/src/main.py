"""Telegram Bot - Main entry point."""

import asyncio
import logging
import os
import sys

import httpx
import structlog
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

# Add shared to path
sys.path.insert(0, "/app")
from shared.logging_config import setup_logging

from .agent_manager import agent_manager
from .clients.api import api_client
from .clients.workers_spawner import workers_spawner
from .config import get_settings
from .handlers import handle_callback_query
from .keyboards import main_menu_keyboard
from .middleware import auth_middleware, is_admin

logger = structlog.get_logger()

# ... existing start/menu handlers ... (keep them if they were imported or define them here)
# Actually, I should keep the existing start/menu functions.
# I will copy them from the original file content below.


async def _post_rag_message(payload: dict) -> None:
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
    is_admin = tg_user.id in admin_ids

    payload = {
        "telegram_id": tg_user.id,
        "username": tg_user.username,
        "first_name": tg_user.first_name,
        "last_name": tg_user.last_name,
        "is_admin": is_admin,
    }

    headers = {"X-Telegram-ID": str(tg_user.id)}

    try:
        await api_client.post_json("users/upsert", headers=headers, json=payload)
    except httpx.HTTPError as e:
        logger.warning("user_registration_failed", error=str(e))


async def handle_message(update: Update, context) -> None:
    """Handle incoming messages - send to AgentManager and reply with response."""
    # Auth check
    if not await auth_middleware(update, context):
        return

    # Ensure user is registered in DB
    if update.effective_user:
        await _ensure_user_registered(update.effective_user)

    user_id = update.effective_user.id
    message_id = update.message.message_id
    text = update.message.text

    logger.info("message_received", user_id=user_id, text_length=len(text) if text else 0)

    # Indicate typing status
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        # Log user message to RAG
        # We do this asynchronously to not block
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

        # Send to agent and wait for response (headless mode)
        response_text = await agent_manager.send_message(user_id, text)

        # Send response to user
        try:
            await update.message.reply_text(
                response_text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            # Fallback to plain text if markdown fails
            await update.message.reply_text(response_text)

    except Exception as e:
        logger.error("message_handling_failed", error=str(e), user_id=user_id)
        await update.message.reply_text("⚠️ Ошибка обработки сообщения. Попробуйте позже.")


async def post_init(app: Application) -> None:
    """Post-initialization: connect clients."""
    await workers_spawner.connect()
    logger.info("Telegram bot initialized")


async def post_shutdown(app: Application) -> None:
    """Cleanup on shutdown."""
    await workers_spawner.close()
    await agent_manager.close()
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
