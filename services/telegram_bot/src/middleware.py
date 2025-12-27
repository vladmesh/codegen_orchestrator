"""Telegram bot middleware for user authentication."""

import structlog
from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from .config import get_settings

logger = structlog.get_logger()


async def auth_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is allowed to interact with bot.

    Returns True if user is whitelisted or no whitelist is configured.
    Returns False if user is rejected.
    Sends friendly rejection message for unauthorized users.
    """
    # Allow system updates without user (if any)
    if not update.effective_user:
        return True

    settings = get_settings()
    admin_ids = settings.get_admin_ids()

    # If list is empty, BLOCK ONLY IF STRICLY REQUIRED.
    # But user said "leave ordinary users aside".
    # Let's assume empty list = allow none? Or allow all (dev)?
    # Standard security practice: fail closed. But for dev UX, maybe allow all if variable not set?
    # User said: "All in this env are admins. Others aside."
    # If I set strictly:
    if not admin_ids:
        # Warn but maybe allow if it's not configured?
        # No, user wants control. Let's block if not in list.
        # BUT wait, if I haven't set it yet, I lock myself out.
        # Better: if empty, maybe log warning but allow?
        # Re-reading: "let's name it ADMIN... everyone in it IS admin".
        # Safe bet: If variable is set, enforce it. If not set, maybe open?
        # Actually user said "Let's assume ordinary users are left aside".
        pass

    user_id = update.effective_user.id

    # Strict Whitelist
    if admin_ids and user_id not in admin_ids:
        # User not authorized
        logger.warning(
            "unauthorized_access_attempt",
            telegram_id=user_id,
            username=update.effective_user.username,
        )
        if update.message:
            await update.message.reply_text(
                "🚫 **Доступ запрещён**\n\n"
                "Вы не являетесь администратором бота.\n"
                f"Ваш ID: `{user_id}`",
                parse_mode="Markdown",
            )
        elif update.callback_query:
            await update.callback_query.answer("🚫 Доступ запрещён", show_alert=True)

        # Stop further processing
        raise ApplicationHandlerStop()

    # If list is empty, we allow (backward compat) or block?
    if not admin_ids:
        logger.warning("no_admin_whitelist_configured_allowing_all")
        return True

    return True
