"""Entry point for the Telegram bot service.

This module wires a minimal python-telegram-bot application that can be
extended with real handlers later on.
"""

from __future__ import annotations

from datetime import UTC, datetime
import os
import time
from typing import Final

import structlog
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from services.tg_bot.src.access import TELEGRAM_CHANNEL, is_active, telegram_external_id
from shared.generated.events import get_broker, publish_command_received
from shared.generated.schemas import CommandReceived, UserAccess
from shared.http_client import ServiceClient
from shared.logging import configure_logging

configure_logging(service_name="tg_bot")
LOGGER = structlog.stdlib.get_logger()

DEFAULT_GREETING: Final[str] = (
    "Привет! Мы Service Template и этот бот помогает подключиться к нашему сервису."
)
PLACEHOLDER_TELEGRAM_BOT_TOKEN: Final[str] = "your-telegram-bot-token"  # noqa: S105
ALLOW_PLACEHOLDER_TOKEN_ENV: Final[str] = "TG_BOT_ALLOW_PLACEHOLDER_TOKEN"  # noqa: S105

class BackendClient(ServiceClient):
    """Typed HTTP client for the backend service."""

    def __init__(self) -> None:
        super().__init__(base_url_env="BACKEND_API_URL")

    async def resolve(self, channel: str, external_id: str) -> UserAccess:
        resp = await self._request(
            "get", "/users/access", params={"channel": channel, "external_id": external_id}
        )
        return UserAccess.model_validate(resp.json())


def _get_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set; please add it to your environment "
            "or docker-compose overlay before running the bot."
        )
    return token


def _uses_placeholder_token() -> bool:
    return os.getenv("TELEGRAM_BOT_TOKEN") == PLACEHOLDER_TELEGRAM_BOT_TOKEN


def _allows_placeholder_token() -> bool:
    return os.getenv(ALLOW_PLACEHOLDER_TOKEN_ENV) == "true"


async def _has_active_access(telegram_id: int | None) -> bool:
    """Resolve the Telegram identity and accept only an active backend user."""

    try:
        external_id = telegram_external_id(telegram_id)
        if external_id is None:
            return False
        async with BackendClient() as client:
            access = await client.resolve(TELEGRAM_CHANNEL, external_id)
        return is_active(access.status)
    except Exception:
        # Resolution, parsing, and model failures must never bypass admission.
        return False


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply to the /start command with a friendly greeting."""

    telegram_user = update.effective_user
    if telegram_user is None or telegram_user.id is None:
        LOGGER.warning("/start received without a valid Telegram user")
        return
    reply_text = f"{DEFAULT_GREETING}\nДобро пожаловать, {telegram_user.first_name or 'друг'}!"
    if update.message:
        await update.message.reply_text(reply_text)
    LOGGER.info("Handled /start for user_id=%s", telegram_user.id)


async def handle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /command and publish event directly to Redis."""
    telegram_user = update.effective_user
    if telegram_user is None or update.message is None:
        return

    command = update.message.text or "/command"
    args = context.args or []

    event = CommandReceived(
        command=command,
        args=args,
        user_id=telegram_user.id,
        timestamp=datetime.now(UTC),
    )

    try:
        await publish_command_received(event)
        await update.message.reply_text("Command published!")
        LOGGER.info("Published command event: %s", event.command)
    except Exception:
        LOGGER.exception("Failed to publish command event")
        await update.message.reply_text("Failed to send command.")


async def post_init(application: Application) -> None:
    """Connect to Redis broker after application init."""
    await get_broker().connect()
    LOGGER.info("Connected to Redis broker")


async def post_shutdown(application: Application) -> None:
    """Disconnect from Redis broker on shutdown."""
    await get_broker().close()
    LOGGER.info("Disconnected from Redis broker")


async def enforce_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop updates whose Telegram identity lacks active backend access.

    Runs before every handler. Raising ``ApplicationHandlerStop`` keeps the update
    from reaching any of them, so access does not depend on each handler
    remembering to check.
    """


    try:
        user = update.effective_user
        has_access = await _has_active_access(user.id if user else None)
    except Exception:
        has_access = False
    if has_access:
        return

    try:
        LOGGER.info("access_denied")
    except Exception:  # noqa: S110
        pass
    raise ApplicationHandlerStop


def build_application() -> Application:
    """Create the telegram bot application with all handlers wired in."""
    from services.tg_bot.src.middleware import install_update_logging


    application = (
        ApplicationBuilder()
        .token(_get_token())
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(TypeHandler(Update, enforce_access), group=-1)
    application.add_handler(CommandHandler("start", handle_start))
    application.add_handler(CommandHandler("command", handle_command))

    install_update_logging(application)
    return application


def main() -> None:
    """Run the bot until the process receives a termination signal."""

    if _uses_placeholder_token():
        if not _allows_placeholder_token():
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN is still the placeholder value; set a real token "
                f"or enable {ALLOW_PLACEHOLDER_TOKEN_ENV}=true only in dev compose."
            )
        LOGGER.warning(
            "TELEGRAM_BOT_TOKEN is still the placeholder value; "
            "tg_bot stays idle until a real token is configured."
        )
        while True:
            time.sleep(3600)

    application = build_application()
    LOGGER.info("Starting telegram bot polling loop")
    application.run_polling()


if __name__ == "__main__":
    main()
