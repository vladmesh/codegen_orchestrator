"""Telegram bot middleware for user authentication.

Two-tier authorization:
1. Admins (from ADMIN_TELEGRAM_IDS env) - full access, is_admin=True
2. Regular users (created by admin in DB) - basic access, is_admin=False
3. Everyone else - blocked (fail-closed)
"""

import httpx
import structlog
from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from .clients.api import api_client
from .config import get_settings

logger = structlog.get_logger()

# Context key for storing user info
USER_IS_ADMIN_KEY = "user_is_admin"


async def _check_user_in_db(telegram_id: int) -> dict | None:
    """Check if user exists in database via API.

    Returns user dict if found, None otherwise.
    """
    try:
        return await api_client.get_json(f"users/by-telegram/{telegram_id}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == httpx.codes.NOT_FOUND:
            return None
        logger.warning("user_check_failed", telegram_id=telegram_id, error=str(e))
        return None
    except httpx.HTTPError as e:
        logger.warning("user_check_failed", telegram_id=telegram_id, error=str(e))
        return None


async def auth_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is allowed to interact with bot.

    Authorization logic (fail-closed):
    1. If telegram_id in ADMIN_TELEGRAM_IDS env → admin, full access
    2. If telegram_id exists in DB → regular user, basic access
    3. Otherwise → blocked

    Sets context.user_data[USER_IS_ADMIN_KEY] = True/False for downstream handlers.

    Returns True if user is authorized, False otherwise.
    """
    # Allow system updates without user (if any)
    if not update.effective_user:
        return True

    user_id = update.effective_user.id
    settings = get_settings()
    admin_ids = settings.get_admin_ids()

    # Check 1: Is user an admin (from env)?
    if admin_ids and user_id in admin_ids:
        context.user_data[USER_IS_ADMIN_KEY] = True
        logger.debug("admin_access_granted", telegram_id=user_id)
        return True

    # Check 2: Is user registered in DB?
    db_user = await _check_user_in_db(user_id)
    if db_user:
        # User exists in DB - grant access based on their is_admin flag
        is_admin = db_user.get("is_admin", False)
        context.user_data[USER_IS_ADMIN_KEY] = is_admin
        logger.debug(
            "user_access_granted",
            telegram_id=user_id,
            is_admin=is_admin,
            source="database",
        )
        return True

    # Check 3: Fail-closed - block unknown users
    logger.warning(
        "unauthorized_access_attempt",
        telegram_id=user_id,
        username=update.effective_user.username,
    )

    if update.message:
        await update.message.reply_text(
            "🚫 **Доступ запрещён**\n\n"
            "Вы не зарегистрированы в системе.\n"
            "Обратитесь к администратору для получения доступа.\n\n"
            f"Ваш ID: `{user_id}`",
            parse_mode="Markdown",
        )
    elif update.callback_query:
        await update.callback_query.answer("🚫 Доступ запрещён", show_alert=True)

    # Stop further processing
    raise ApplicationHandlerStop()


def is_admin(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if current user is admin.

    Use this in handlers to check permissions.
    """
    return context.user_data.get(USER_IS_ADMIN_KEY, False)
