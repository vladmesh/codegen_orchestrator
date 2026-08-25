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


def _promo_from_update(update: Update) -> str | None:
    """Treat an unknown user's text as a code, including `/start <code>`."""
    text = update.message.text if update.message else None
    if not text:
        return None
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        return parts[1] if len(parts) > 1 else None
    return text


async def _upsert_user(tg_user, promo_code: str | None = None) -> bool:
    """Ask the API to register an owner or redeem an unknown user's code."""
    settings = get_settings()
    payload = {
        "telegram_id": tg_user.id,
        "username": tg_user.username,
        "first_name": tg_user.first_name,
        "last_name": tg_user.last_name,
        "is_admin": tg_user.id in settings.get_admin_ids(),
    }
    if promo_code:
        payload["promo_code"] = promo_code
    try:
        await api_client.post_json(
            "users/upsert", headers={"X-Telegram-ID": str(tg_user.id)}, json=payload
        )
        return True
    except httpx.HTTPError as error:
        logger.warning("user_registration_failed", telegram_id=tg_user.id, error=str(error))
        return False


async def auth_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is allowed to interact with bot.

    Authorization logic (fail-closed):
    1. If telegram_id in ADMIN_TELEGRAM_IDS env → admin, full access
    2. If telegram_id exists in DB → regular user, basic access
    3. Otherwise → blocked

    The database lookup is the sole source of truth for ordinary registration.
    An unknown update is consumed here as a promo attempt and never reaches a
    downstream handler.

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
        if not await _upsert_user(update.effective_user):
            raise ApplicationHandlerStop()
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
    logger.info(
        "unregistered_user_pending_promo",
        telegram_id=user_id,
        username=update.effective_user.username,
    )

    promo_code = _promo_from_update(update)
    if promo_code and await _upsert_user(update.effective_user, promo_code):
        await update.message.reply_text("Промокод активирован. Добро пожаловать!")
    elif update.message:
        await update.message.reply_text("Чтобы начать, пришлите одноразовый промокод.")
    elif update.callback_query:
        await update.callback_query.answer("Сначала активируйте промокод", show_alert=True)
    raise ApplicationHandlerStop()


def is_admin(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if current user is admin.

    Use this in handlers to check permissions.
    """
    return context.user_data.get(USER_IS_ADMIN_KEY, False)
