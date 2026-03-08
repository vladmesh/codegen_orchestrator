"""Notification service for sending Telegram messages to admins.

Shared utility used by both API and LangGraph services.
Configuration is loaded lazily on first use — importing this module
does NOT require env vars to be set.
"""

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
import os

import aiohttp
import structlog

logger = structlog.get_logger(__name__)

# Lazy config — populated on first call to _ensure_config()
_config: dict | None = None


def _ensure_config() -> dict:
    """Load and validate config on first use. Raises RuntimeError if missing."""
    global _config  # noqa: PLW0603
    if _config is not None:
        return _config

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not telegram_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    api_url = os.getenv("API_BASE_URL")
    if not api_url:
        raise RuntimeError("API_BASE_URL is not set")

    if api_url.rstrip("/").endswith("/api"):
        raise RuntimeError("API_BASE_URL must not include /api suffix")

    rate_limit = int(os.getenv("NOTIFICATION_RATE_LIMIT", "10"))

    _config = {
        "telegram_token": telegram_token,
        "api_url": api_url,
        "rate_limit": rate_limit,
    }
    return _config


# Rate limiting storage (in-memory, simple MVP)
_rate_limit_storage: dict[int, list[datetime]] = defaultdict(list)

# Emoji mapping for severity levels
EMOJI_MAP = {
    "info": "ℹ️",
    "warning": "⚠️",
    "error": "❌",
    "critical": "🚨",
    "success": "✅",
}


async def send_telegram_message(
    telegram_id: int,
    text: str,
    parse_mode: str = "Markdown",
) -> bool:
    """Send a message to a Telegram user via Bot API.

    Args:
        telegram_id: Telegram user ID
        text: Message text
        parse_mode: Parse mode (Markdown or HTML)

    Returns:
        True if sent successfully, False otherwise

    Raises:
        RuntimeError: If TELEGRAM_BOT_TOKEN is not set
    """
    config = _ensure_config()

    # Check rate limit
    if not _check_rate_limit(telegram_id):
        logger.warning("rate_limit_exceeded", telegram_id=telegram_id, action="skip_notification")
        return False

    url = f"https://api.telegram.org/bot{config['telegram_token']}/sendMessage"
    payload = {
        "chat_id": telegram_id,
        "text": text,
        "parse_mode": parse_mode,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == HTTPStatus.OK:
                    logger.info("notification_sent", telegram_id=telegram_id)
                    _record_message(telegram_id)
                    return True
                else:
                    error_text = await resp.text()
                    logger.error(
                        "notification_failed",
                        telegram_id=telegram_id,
                        status=resp.status,
                        error=error_text,
                    )
                    return False
    except TimeoutError:
        logger.error("notification_timeout", telegram_id=telegram_id)
        return False
    except Exception as e:
        logger.error("notification_error", telegram_id=telegram_id, error=str(e))
        return False


async def notify_admins(message: str, level: str = "info") -> int:
    """Notify all admin users via Telegram.

    Args:
        message: Message text (will be prefixed with emoji)
        level: Severity level (info, warning, error, critical, success)

    Returns:
        Number of users successfully notified

    Raises:
        RuntimeError: If TELEGRAM_BOT_TOKEN or API_BASE_URL is not set
    """
    config = _ensure_config()

    # Get all users from API
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{config['api_url']}/api/users", timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != HTTPStatus.OK:
                    logger.error("fetch_users_failed", status=resp.status)
                    return 0

                users = await resp.json()
    except Exception as e:
        logger.error("fetch_users_error", error=str(e))
        return 0

    if not users:
        logger.warning("no_users_found", action="skip_notifications")
        return 0

    # Filter admin users
    admin_users = [u for u in users if u.get("is_admin")]

    if not admin_users:
        logger.warning("no_admin_users_found", action="skip_notifications")
        return 0

    # Prepare message with emoji
    emoji = EMOJI_MAP.get(level, "ℹ️")
    formatted_message = f"{emoji} {message}"

    # Send to all admins in parallel
    tasks = [send_telegram_message(user["telegram_id"], formatted_message) for user in admin_users]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Count successes
    success_count = sum(1 for r in results if r is True)

    logger.info(
        "admins_notified",
        success_count=success_count,
        total_admins=len(admin_users),
        level=level,
    )

    return success_count


def _check_rate_limit(telegram_id: int) -> bool:
    """Check if user is within rate limit."""
    config = _ensure_config()
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=1)

    _rate_limit_storage[telegram_id] = [
        ts for ts in _rate_limit_storage[telegram_id] if ts > cutoff
    ]

    return len(_rate_limit_storage[telegram_id]) < config["rate_limit"]


def _record_message(telegram_id: int):
    """Record a sent message for rate limiting."""
    _rate_limit_storage[telegram_id].append(datetime.now(UTC))
