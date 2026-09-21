"""Notification service for sending Telegram messages to admins.

Shared utility used by both API and LangGraph services.
Configuration is loaded lazily on first use — importing this module
does NOT require env vars to be set.
"""

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from http import HTTPStatus
import os

import aiohttp
from pydantic import TypeAdapter
import structlog

from shared.clients.internal_api import InternalAPIClient
from shared.contracts.dto.user import UserDTO

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

                error_text = await resp.text()

                # Retry without parse_mode if Telegram can't parse entities
                if (
                    resp.status == HTTPStatus.BAD_REQUEST
                    and "can't parse entities" in error_text
                    and parse_mode
                ):
                    logger.warning(
                        "notification_parse_retry",
                        telegram_id=telegram_id,
                        parse_mode=parse_mode,
                    )
                    plain_payload = {
                        "chat_id": telegram_id,
                        "text": text,
                    }
                    async with session.post(
                        url, json=plain_payload, timeout=aiohttp.ClientTimeout(total=10)
                    ) as retry_resp:
                        if retry_resp.status == HTTPStatus.OK:
                            logger.info("notification_sent", telegram_id=telegram_id)
                            _record_message(telegram_id)
                            return True
                        retry_error = await retry_resp.text()
                        logger.error(
                            "notification_failed",
                            telegram_id=telegram_id,
                            status=retry_resp.status,
                            error=retry_error,
                        )
                        return False

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


class AdminDeliveryStatus(StrEnum):
    """What one publication to the administrator audience amounted to."""

    #: Every configured administrator's Telegram send returned success.
    DELIVERED = "delivered"
    #: Some, but not all, configured administrators were reached.
    PARTIAL = "partial"
    #: At least one administrator is configured and none was reached.
    FAILED = "failed"
    #: No administrator is configured. Nobody can be told; not a delivery.
    UNADDRESSABLE = "unaddressable"


@dataclass(frozen=True)
class AdminDeliveryResult:
    """The per-recipient truth `notify_admins` folds into one success count.

    ``send_telegram_message`` reports rate limiting, non-200 answers, timeouts
    and transport errors as ``False`` rather than raising, so a caller that has
    to settle a durable obligation cannot read "did not raise" as delivered.
    """

    configured: int
    succeeded: int

    @property
    def status(self) -> AdminDeliveryStatus:
        if self.configured == 0:
            return AdminDeliveryStatus.UNADDRESSABLE
        if self.succeeded >= self.configured:
            return AdminDeliveryStatus.DELIVERED
        if self.succeeded == 0:
            return AdminDeliveryStatus.FAILED
        return AdminDeliveryStatus.PARTIAL

    @property
    def detail(self) -> str:
        return (
            f"{self.status.value}: Telegram accepted {self.succeeded} of "
            f"{self.configured} configured administrators"
        )


async def _list_admin_users() -> list[UserDTO]:
    """Read the administrators from the users API. Propagates every read failure."""
    config = _ensure_config()

    # Get all users through the shared transport, so this read carries
    # X-Internal-Key and X-Correlation-ID like every other internal API call.
    client = InternalAPIClient(config["api_url"], timeout=5.0)
    try:
        resp = await client.get_raw("users")
        if resp.status_code != HTTPStatus.OK:
            raise RuntimeError(f"users API returned HTTP {resp.status_code}")
        users = TypeAdapter(list[UserDTO]).validate_python(resp.json())
    finally:
        await client.close()

    if not users:
        logger.warning("no_users_found", action="skip_notifications")
        return []

    admin_users = [user for user in users if user.is_admin]
    if not admin_users:
        logger.warning("no_admin_users_found", action="skip_notifications")
    return admin_users


async def deliver_to_admins(message: str, level: str = "info") -> AdminDeliveryResult:
    """Send one message to every administrator and report per-recipient truth.

    This is the boundary for callers that settle a durable obligation. It raises
    exactly what `notify_admins` raises (configuration and users-API failures);
    Telegram failures are returned in the result, never swallowed into success.
    """
    admin_users = await _list_admin_users()
    if not admin_users:
        return AdminDeliveryResult(configured=0, succeeded=0)

    emoji = EMOJI_MAP.get(level, "ℹ️")
    formatted_message = f"{emoji} {message}"

    # Send to all admins in parallel
    tasks = [send_telegram_message(user.telegram_id, formatted_message) for user in admin_users]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    result = AdminDeliveryResult(
        configured=len(admin_users), succeeded=sum(1 for r in results if r is True)
    )
    logger.info(
        "admins_notified",
        success_count=result.succeeded,
        total_admins=result.configured,
        level=level,
    )
    return result


async def notify_admins(message: str, level: str = "info") -> int:
    """Notify all administrators via Telegram and propagate boundary failures.

    Args:
        message: Message text (will be prefixed with emoji)
        level: Severity level (info, warning, error, critical, success)

    Returns:
        Number of administrators whose Telegram delivery succeeded. A return of
        zero is valid when the users API returns no administrators. Callers that
        must tell no administrators from failed delivery use `deliver_to_admins`.

    Raises:
        RuntimeError: If required configuration is missing or the users API
            returns a non-200 response.
        httpx.RequestError: If the users API request fails or times out.
        ValidationError: If the users API response is not a valid user list.
    """
    return (await deliver_to_admins(message, level=level)).succeeded


async def notify_admins_best_effort(
    message: str,
    level: str = "info",
    **context: object,
) -> None:
    """Send an outcome-independent admin alert without raising.

    This is the scheduler and workflow boundary for alerts emitted after state
    has been committed. Delivery can be lost; this function logs one safe
    failure record and returns ``None``. It does not provide retries, an outbox,
    or guaranteed Telegram delivery.
    """
    try:
        await notify_admins(message, level=level)
    except Exception as exc:
        logger.error(
            "admin_notification_failed",
            level=level,
            error_type=type(exc).__name__,
            **context,
        )


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
