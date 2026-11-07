"""Telegram Bot - Main entry point.

Direct PO ReactAgent communication via Redis Streams.
Messages flow: XADD po:input → PO consumer → XREAD po:response:{request_id}.
"""

import asyncio
import logging
import math
import os
import sys
import time
import uuid

import httpx
import redis.asyncio as redis_lib
import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from shared.config_store import ConfigStore, ConfigStoreUnavailableError
from shared.contracts.queues.po import (
    POPayloadProtectionError,
    POResponse,
    POUserMessage,
    to_flat_fields,
    unprotect_po_payload,
)
from shared.engineering_budget_display import format_microusd
from shared.queues import PO_INPUT_QUEUE, PO_PROACTIVE_GROUP, PO_PROACTIVE_QUEUE
from shared.redis import RedisStreamClient
from shared.redis.po import verify_po_storage

# Add shared to path
sys.path.insert(0, "/app")
from shared.log_config import setup_logging  # noqa: E402

from .clients.api import api_client  # noqa: E402
from .config import get_settings  # noqa: E402
from .dashboard import mint_dashboard_url  # noqa: E402
from .handlers import cancel_add_user, handle_add_user_input, handle_callback_query  # noqa: E402
from .keyboards import main_menu_keyboard  # noqa: E402
from .middleware import auth_middleware, is_admin  # noqa: E402
from .notifications import ProvisionerNotifier  # noqa: E402
from .po_delivery import (  # noqa: E402
    POResponseListener,
    POResponsePending,
    deliver_response,
    is_settled,
    register_request,
)
from .proactive import (  # noqa: E402
    PROACTIVE_RECLAIM_IDLE_MS,
    process_proactive_entry,
)
from .texts import text, user_locale  # noqa: E402
from .update_processor import UserUpdateProcessor  # noqa: E402

logger = structlog.get_logger()

# Globals (initialized in post_init)
_provisioner_notifier_task: asyncio.Task | None = None
_proactive_listener_task: asyncio.Task | None = None
_po_response_listener_task: asyncio.Task | None = None
_stream_client: RedisStreamClient | None = None
_config_store: ConfigStore | None = None
_config_refresh_task: asyncio.Task | None = None


def get_stream_client() -> RedisStreamClient:
    """Get the global Redis stream client. Raises if not initialized."""
    if _stream_client is None:
        raise RuntimeError("Redis client not initialized")
    return _stream_client


PO_RESPONSE_TIMEOUT_KEY = "telegram.po_response_timeout_seconds"
# Pause after a failed read of the PO response stream before the next read.
PO_RESPONSE_READ_RETRY_DELAY_S = 0.5
TYPING_INTERVAL_S = 5


async def start(update: Update, context) -> None:
    """Handle /start command - show main menu."""
    user_is_admin = is_admin(context)
    locale = user_locale(update)
    await update.message.reply_text(
        text(locale, "start_html"),
        reply_markup=main_menu_keyboard(locale, is_admin=user_is_admin),
        parse_mode=ParseMode.HTML,
    )


async def menu(update: Update, context) -> None:
    """Handle /menu command - show main menu."""
    user_is_admin = is_admin(context)
    locale = user_locale(update)
    await update.message.reply_text(
        text(locale, "menu_html"),
        reply_markup=main_menu_keyboard(locale, is_admin=user_is_admin),
        parse_mode=ParseMode.HTML,
    )


async def dashboard(update: Update, context) -> None:
    """Handle /dashboard command — generate one-time token and send dashboard URL."""
    telegram_id = update.effective_user.id
    locale = user_locale(update)

    url = await mint_dashboard_url(get_stream_client().redis, telegram_id)
    if url is None:
        await update.message.reply_text(text(locale, "no_projects"))
        return

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(text(locale, "btn_open_dashboard"), url=url)]]
    )
    await update.message.reply_text(
        text(locale, "dashboard_link"),
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


async def balance(update: Update, context) -> None:
    """Show the authenticated user's ledger-derived engineering balance."""
    telegram_id = update.effective_user.id
    locale = user_locale(update)
    try:
        data = await api_client.get_json(
            "engineering-budget-policy/balance",
            headers={"X-Telegram-ID": str(telegram_id)},
        )
    except httpx.HTTPError as error:
        logger.warning(
            "engineering_budget_balance_failed", telegram_id=telegram_id, error=str(error)
        )
        await update.message.reply_text(text(locale, "balance_failed"))
        return

    lines = [
        text(locale, "balance_title"),
        "",
        text(locale, "balance_spent", amount=format_microusd(data["known_spend_microusd"])),
    ]
    enforcement = data["enforcement"]
    if enforcement == "enforced":
        lines.append(
            text(locale, "balance_available", amount=format_microusd(data["remaining_microusd"]))
        )
        if data["exhausted"]:
            lines.append(text(locale, "balance_exhausted"))
    elif enforcement == "not_enforced":
        lines.append(text(locale, "balance_not_enforced"))
    else:
        lines.append(text(locale, "balance_unlimited"))

    unknown_attempts = data["unknown_cost_attempt_count"]
    if unknown_attempts:
        lines.extend(["", text(locale, "balance_unknown_cost", count=unknown_attempts)])
    elif data["incomplete_coverage"]:
        lines.extend(["", text(locale, "balance_incomplete")])

    await update.message.reply_text("\n".join(lines))


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
) -> POResponse | None:
    """Read PO response from a per-request stream.

    Uses id="0" to read from beginning — catches response even if
    written before XREAD starts (no race condition).

    Args:
        redis: Redis client
        response_stream: Stream name (po:response:{request_id})
        timeout_s: Max wait time in seconds

    Returns:
        The PO response, or None on timeout
    """
    start_time = time.monotonic()

    while True:
        elapsed = time.monotonic() - start_time
        remaining = timeout_s - elapsed

        if remaining <= 0:
            return None

        # Redis treats BLOCK 0 as an indefinite wait.
        block_ms = 2000 if remaining >= 2 else max(1, math.ceil(remaining * 1000))

        try:
            messages = await redis.xread(
                {response_stream: "0"},
                count=1,
                block=block_ms,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("po_response_xread_error", error_type=type(e).__name__)
            await asyncio.sleep(PO_RESPONSE_READ_RETRY_DELAY_S)
            continue

        if not messages:
            continue

        for _stream_name, stream_messages in messages:
            if stream_messages:
                _msg_id, data = stream_messages[0]
                logical = unprotect_po_payload(response_stream, data)
                try:
                    return POResponse.model_validate(logical)
                except Exception:
                    raise POPayloadProtectionError("PO response validation failed") from None

    return None


async def _send_to_po_and_wait(
    client: RedisStreamClient,
    telegram_chat_id: int,
    text: str,
    bot,
    chat_id: int,
    user_name: str = "",
    locale: str = "en",
) -> tuple[str, POResponse]:
    """Record an owed PO input and wait up to the interactive deadline.

    The accepted request remains owed if waiting ends or is cancelled.
    """
    if _config_store is None:
        raise RuntimeError("Telegram system config is not initialized")
    try:
        value = _config_store.get(PO_RESPONSE_TIMEOUT_KEY)
    except KeyError as exc:
        raise RuntimeError(f"Missing required system config: {PO_RESPONSE_TIMEOUT_KEY}") from exc
    if isinstance(value, bool):
        raise ValueError(f"{PO_RESPONSE_TIMEOUT_KEY} must be positive finite seconds")
    try:
        wait_seconds = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{PO_RESPONSE_TIMEOUT_KEY} must be positive finite seconds") from exc
    if not math.isfinite(wait_seconds) or wait_seconds <= 0:
        raise ValueError(f"{PO_RESPONSE_TIMEOUT_KEY} must be positive finite seconds")

    request_id = str(uuid.uuid4())
    response_stream = f"po:response:{request_id}"

    msg = POUserMessage(
        text=text,
        telegram_chat_id=str(telegram_chat_id),
        request_id=request_id,
        user_name=user_name,
    )
    await register_request(client.redis, request_id, telegram_chat_id, locale, wait_seconds)
    await client.publish_flat(PO_INPUT_QUEUE, to_flat_fields(msg))

    logger.info(
        "po_message_sent",
        telegram_chat_id=telegram_chat_id,
        request_id=request_id,
    )

    typing_task = asyncio.create_task(_keep_typing(bot, chat_id))

    try:
        response = await _read_po_response(client.redis, response_stream, wait_seconds)

        if response is None:
            raise POResponsePending(request_id)

        return request_id, response

    finally:
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass


async def handle_message(update: Update, context) -> None:
    """Handle incoming messages - send to PO ReactAgent via Redis Streams."""
    global _stream_client

    # Check if admin is in "add user" flow — handle separately
    if context.user_data.get("awaiting_add_user"):
        await handle_add_user_input(update, context)
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message_text = update.message.text

    logger.info(
        "message_received",
        user_id=user_id,
        text_length=len(message_text) if message_text else 0,
    )

    try:
        if _stream_client is None:
            raise RuntimeError("Redis client not initialized")

        # Send to PO and wait for response
        user_name = update.effective_user.first_name or ""
        request_id, response = await _send_to_po_and_wait(
            client=_stream_client,
            telegram_chat_id=user_id,
            text=message_text,
            bot=context.bot,
            chat_id=chat_id,
            user_name=user_name,
            locale=user_locale(update),
        )

        delivered = await deliver_response(_stream_client.redis, context.bot, request_id, response)
        if not delivered and not await is_settled(_stream_client.redis, request_id):
            await update.message.reply_text(text(user_locale(update), "po_timeout"))

    # What the user sees when their message could not be answered is fixed on
    # purpose: exception text is for the logs, never for the chat.
    except POResponsePending as pending:
        logger.info("po_response_pending", user_id=user_id, request_id=pending.request_id)
        if not await is_settled(_stream_client.redis, pending.request_id):
            await update.message.reply_text(text(user_locale(update), "po_timeout"))
    except RuntimeError as e:
        logger.error("po_response_error", error_type=type(e).__name__, user_id=user_id)
        await update.message.reply_text(text(user_locale(update), "message_failed"))
    except Exception as e:
        logger.error("message_handling_failed", error_type=type(e).__name__, user_id=user_id)
        await update.message.reply_text(text(user_locale(update), "message_failed"))


class ProactiveListener:
    """Listens to po:proactive stream and sends messages to users.

    Every entry is settled by ``process_proactive_entry``, which owns the ack:
    delivered, or out of bounded attempts with an admin alert naming the story,
    project and event. Entries are consumed without auto-ack and the pending ones
    left by a previous incarnation are claimed on startup, so a bot that died
    while calling Telegram picks the notification back up instead of losing it.
    """

    def __init__(self, client: RedisStreamClient):
        self.client = client
        self._running = False

    async def start(self, bot) -> asyncio.Task:
        """Start the listener background task."""
        self._running = True
        return asyncio.create_task(self._listen_loop(bot))

    async def stop(self) -> None:
        """Stop the listener."""
        self._running = False

    async def _listen_loop(self, bot) -> None:
        """Main loop: read po:proactive, send messages to users."""
        logger.info("proactive_listener_started")

        try:
            async for msg in self.client.consume(
                PO_PROACTIVE_QUEUE,
                PO_PROACTIVE_GROUP,
                "bot-0",
                count=10,
                auto_ack=False,
                claim_pending=True,
                pending_timeout_ms=PROACTIVE_RECLAIM_IDLE_MS,
            ):
                if not self._running:
                    break
                if msg is None:
                    continue
                await process_proactive_entry(bot, self.client, msg)
        except asyncio.CancelledError:
            pass

        logger.info("proactive_listener_stopped")


async def post_init(app: Application) -> None:
    """Post-initialization: connect Redis and start listeners."""
    global _provisioner_notifier_task, _proactive_listener_task
    global _po_response_listener_task, _stream_client, _config_store, _config_refresh_task

    settings = get_settings()

    # Single RedisStreamClient for all operations (message handling + consumer listeners)
    _stream_client = RedisStreamClient(redis_url=settings.redis_url)
    await _stream_client.connect()
    await verify_po_storage(_stream_client.redis)

    # Start provisioner notifications listener
    admin_ids = settings.get_admin_ids()
    notifier = ProvisionerNotifier(client=_stream_client, admin_ids=admin_ids)
    _provisioner_notifier_task = await notifier.start(app.bot)

    # Start proactive PO messages listener
    proactive = ProactiveListener(client=_stream_client)
    _proactive_listener_task = await proactive.start(app.bot)
    _po_response_listener_task = asyncio.create_task(
        POResponseListener(_stream_client).run(app.bot)
    )

    _config_store = ConfigStore(settings.api_base_url)
    try:
        await _config_store.refresh()
    except ConfigStoreUnavailableError:
        logger.warning("telegram_system_config_unavailable")
    _config_refresh_task = asyncio.create_task(_config_store.refresh_forever())

    logger.info("telegram_bot_initialized", admin_count=len(admin_ids))


async def post_shutdown(app: Application) -> None:
    """Cleanup on shutdown."""
    global _provisioner_notifier_task, _proactive_listener_task
    global _po_response_listener_task, _stream_client, _config_store, _config_refresh_task

    if _config_refresh_task:
        _config_refresh_task.cancel()
        try:
            await _config_refresh_task
        except asyncio.CancelledError:
            pass
        _config_refresh_task = None
    if _config_store:
        await _config_store.close()
        _config_store = None

    # Stop provisioner notifier
    if _provisioner_notifier_task:
        _provisioner_notifier_task.cancel()
        try:
            await _provisioner_notifier_task
        except asyncio.CancelledError:
            pass
        _provisioner_notifier_task = None

    # Stop proactive listener
    if _proactive_listener_task:
        _proactive_listener_task.cancel()
        try:
            await _proactive_listener_task
        except asyncio.CancelledError:
            pass
        _proactive_listener_task = None

    if _po_response_listener_task:
        _po_response_listener_task.cancel()
        try:
            await _po_response_listener_task
        except asyncio.CancelledError:
            pass
        _po_response_listener_task = None

    # Close Redis
    if _stream_client:
        await _stream_client.close()
        _stream_client = None

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
        Application.builder()
        .token(token)
        .concurrent_updates(UserUpdateProcessor(get_settings().telegram_max_concurrent_updates))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("cancel", cancel_add_user))

    # One update-level gate covers messages, commands and callback queries.
    app.add_handler(TypeHandler(Update, auth_middleware), group=-1)

    # Callback query handler for inline buttons
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # Text message handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("telegram_bot_starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
