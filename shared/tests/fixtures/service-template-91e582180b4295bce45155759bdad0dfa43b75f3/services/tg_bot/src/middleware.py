"""Update logging middleware for the Telegram bot.

Wraps ``Application.process_update`` to log every incoming update as a
structured JSON line with standard fields: user_id, update_type, command,
duration_ms.  Errors inside handlers are logged via a dedicated error
handler so the bot process never crashes.
"""

from __future__ import annotations

import time

import structlog
from telegram import Update
from telegram.ext import Application, ContextTypes

logger = structlog.stdlib.get_logger()


def _extract_update_info(update: Update) -> tuple[str | None, str, str | None]:
    """Return ``(user_id, update_type, command)`` from an incoming update."""

    user = update.effective_user
    user_id = f"tg:{user.id}" if user else None

    if update.message and update.message.text and update.message.text.startswith("/"):
        return user_id, "command", update.message.text.split()[0]

    if update.callback_query:
        return user_id, "callback_query", update.callback_query.data

    if update.message:
        return user_id, "message", None

    return user_id, "update", None


async def _log_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Error handler that logs unhandled exceptions from bot handlers."""

    exc = context.error
    extra: dict[str, object] = {
        "exception_type": type(exc).__name__ if exc else "Unknown",
        "exception_message": str(exc) if exc else "",
    }

    if isinstance(update, Update):
        user = update.effective_user
        extra["user_id"] = f"tg:{user.id}" if user else None

    logger.error("handler_error", exc_info=exc, **extra)


def install_update_logging(application: Application) -> None:
    """Monkey-patch *process_update* to log every update with duration."""

    original_process_update = application.process_update

    async def _logged_process_update(update: object) -> None:
        if not isinstance(update, Update):
            return await original_process_update(update)

        start = time.perf_counter()
        user_id, update_type, command = _extract_update_info(update)

        structlog.contextvars.bind_contextvars(
            user_id=user_id,
            update_type=update_type,
            command=command,
        )

        try:
            await original_process_update(update)
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.info("update", duration_ms=duration_ms)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.exception("unhandled_exception", duration_ms=duration_ms)
        finally:
            structlog.contextvars.unbind_contextvars("user_id", "update_type", "command")

    application.process_update = _logged_process_update  # type: ignore[method-assign]
    application.add_error_handler(_log_error)
