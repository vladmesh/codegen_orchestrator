"""Callback query handlers for inline keyboard buttons.

Handles direct API calls without going through LangGraph/LLM.
"""

from collections.abc import Coroutine
from http import HTTPStatus
import re
from typing import Any

import httpx
import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.server import ServerDTO, ServerStatus
from shared.engineering_budget_display import format_microusd

from .clients.api import ProjectDetails, api_client
from .dashboard import mint_dashboard_url
from .keyboards import (
    ACTION_ADD_USER,
    ACTION_BACK,
    ACTION_DEPLOY,
    ACTION_DETAILS,
    ACTION_LIST,
    ACTION_MAINTENANCE,
    ACTION_NEW,
    PREFIX_ADMIN,
    PREFIX_DASHBOARD,
    PREFIX_MENU,
    PREFIX_PROJECT,
    PREFIX_PROJECTS,
    PREFIX_SERVERS,
    back_to_menu_keyboard,
    main_menu_button,
    main_menu_keyboard,
    project_details_keyboard,
    projects_list_keyboard,
    servers_list_keyboard,
)
from .middleware import is_admin
from .texts import text, user_locale

logger = structlog.get_logger()

# The starting budget an invited user is handed. Both are system config, not
# literals: an invite minted with zero credits produces an account that is
# admitted nowhere, and the operator must be able to change the amount without
# a deploy.
INVITE_CREDITS_CONFIG_KEY = "admission.invite_credits_microusd"
INVITE_RESERVATION_CONFIG_KEY = "admission.invite_attempt_reservation_microusd"


class MissingSystemConfigError(RuntimeError):
    """A required system config is absent. The bot refuses to guess a value."""

    def __init__(self, key: str) -> None:
        super().__init__(f"System config '{key}' is not set")
        self.key = key


async def _required_int_config(key: str) -> int:
    """Read one integer system config, or fail — there is no default here."""
    response = await api_client.get_raw(f"system-configs/{key}")
    if response.status_code != HTTPStatus.OK:
        raise MissingSystemConfigError(key)
    return int(response.json()["value"])


def _get_stream_client():
    """Get Redis stream client from main module (lazy import to avoid circular)."""
    from .main import get_stream_client

    return get_stream_client()


def escape_markdown(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    if not text:
        return ""
    # Characters to escape: _ * [ ] ( ) ~ ` > # + - = | { } . !
    escape_chars = r"_*[]()~`>#+-=|{}!."
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", str(text))


async def _api_read[T](read: Coroutine[Any, Any, T]) -> T | None:
    """Await one typed API read; an HTTP failure is logged and reads as None."""
    try:
        return await read
    except httpx.HTTPError as e:
        logger.error("api_request_failed", read=read.__qualname__, error=str(e))
        return None


def _format_project_details(project: ProjectDetails, locale: str) -> str:
    """Format project details for display."""
    status = project.status
    description = project.config.description or text(locale, "no_description")
    repo_url = project.config.repo_url
    deployed_url = project.config.deployed_url

    status_emoji = (
        "✅" if status == ProjectStatus.ACTIVE else "📝" if status == ProjectStatus.DRAFT else "⚪"
    )

    # Escape all dynamic values
    name_esc = escape_markdown(project.title)
    id_esc = escape_markdown(str(project.id))
    status_esc = escape_markdown(status)
    desc_esc = escape_markdown(description)

    lines = [
        f"📦 *{name_esc}* {status_emoji}",
        f"ID: `{id_esc}`",
        f"{text(locale, 'status_label')}: {status_esc}",
        "",
        f"📝 {desc_esc}",
    ]

    if repo_url:
        lines.append(f"🔗 {text(locale, 'repo_label')}: {escape_markdown(repo_url)}")
    if deployed_url:
        lines.append(f"🌐 URL: {escape_markdown(deployed_url)}")

    return "\n".join(lines)


def _format_server_line(server: ServerDTO) -> str:
    """Format single server line."""
    status_emoji = "✅" if server.status in (ServerStatus.READY, ServerStatus.IN_USE) else "⚠️"

    handle_esc = escape_markdown(server.handle)
    status_esc = escape_markdown(server.status)
    ip_esc = escape_markdown(server.public_ip)

    return (
        f"{status_emoji} *{handle_esc}* \\[{status_esc}\\] — {ip_esc} "
        f"\\(RAM: {server.used_ram_mb}/{server.capacity_ram_mb} MB\\)"
    )


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle callback queries from inline buttons."""
    query = update.callback_query
    await query.answer()  # Acknowledge the callback

    data = query.data
    if not data:
        return

    parts = data.split(":")
    prefix = parts[0]
    user_is_admin = is_admin(context)
    locale = user_locale(update)

    logger.info("callback_received", data=data, prefix=prefix, is_admin=user_is_admin)

    try:
        if prefix == PREFIX_DASHBOARD:
            await _handle_dashboard(query, locale)
        elif prefix == PREFIX_MENU:
            await _handle_menu(query, parts, locale, user_is_admin)
        elif prefix == PREFIX_PROJECTS:
            await _handle_projects(query, parts, locale)
        elif prefix == PREFIX_PROJECT:
            await _handle_project(query, parts, locale)
        elif prefix == PREFIX_SERVERS:
            await _handle_servers(query, parts, locale, user_is_admin)
        elif prefix == PREFIX_ADMIN:
            await _handle_admin(query, parts, locale, user_is_admin, context)
        else:
            logger.warning("unknown_callback_prefix", prefix=prefix, data=data)
    except Exception as e:
        logger.error("callback_handler_failed", data=data, error=str(e), exc_info=True)
        await query.edit_message_text(
            text(locale, "action_failed_md"),
            reply_markup=back_to_menu_keyboard(locale),
            parse_mode="MarkdownV2",
        )


async def _handle_dashboard(query, locale: str) -> None:
    """Handle dashboard callback — generate token and send URL."""
    url = await mint_dashboard_url(_get_stream_client().redis, query.from_user.id)
    if url is None:
        await query.edit_message_text(
            text(locale, "no_projects_md"),
            reply_markup=back_to_menu_keyboard(locale),
            parse_mode="MarkdownV2",
        )
        return

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(text(locale, "btn_open_dashboard"), url=url)],
            [main_menu_button(locale)],
        ]
    )
    await query.edit_message_text(
        text(locale, "dashboard_link_md"),
        reply_markup=keyboard,
        parse_mode="MarkdownV2",
    )


async def _handle_menu(query, parts: list[str], locale: str, user_is_admin: bool) -> None:
    """Handle menu callbacks."""
    action = parts[1] if len(parts) > 1 else ACTION_BACK

    if action == ACTION_BACK:
        await query.edit_message_text(
            text(locale, "main_menu_md"),
            reply_markup=main_menu_keyboard(locale, is_admin=user_is_admin),
            parse_mode="MarkdownV2",
        )


async def _handle_projects(query, parts: list[str], locale: str) -> None:
    """Handle projects list callbacks."""
    action = parts[1] if len(parts) > 1 else ACTION_LIST
    telegram_id = query.from_user.id

    if action == ACTION_LIST:
        projects = await _api_read(api_client.list_projects(telegram_id))

        if projects is None:
            await query.edit_message_text(
                text(locale, "projects_load_failed_md"),
                reply_markup=back_to_menu_keyboard(locale),
                parse_mode="MarkdownV2",
            )
            return

        if not projects:
            await query.edit_message_text(
                text(locale, "projects_empty_md"),
                reply_markup=back_to_menu_keyboard(locale),
                parse_mode="MarkdownV2",
            )
            return

        await query.edit_message_text(
            text(locale, "projects_list_md"),
            reply_markup=projects_list_keyboard(projects, locale),
            parse_mode="MarkdownV2",
        )

    elif action == ACTION_NEW:
        await query.edit_message_text(
            text(locale, "new_project_md"),
            reply_markup=back_to_menu_keyboard(locale),
            parse_mode="MarkdownV2",
        )


async def _handle_project(query, parts: list[str], locale: str) -> None:
    """Handle single project callbacks."""
    action = parts[1] if len(parts) > 1 else ""
    project_id = parts[2] if len(parts) > 2 else ""  # noqa: PLR2004 — index into callback_data parts
    telegram_id = query.from_user.id

    if not project_id:
        await query.edit_message_text(
            text(locale, "project_id_missing_md"),
            reply_markup=back_to_menu_keyboard(locale),
            parse_mode="MarkdownV2",
        )
        return

    if action == ACTION_DETAILS:
        project = await _api_read(api_client.get_project(project_id, telegram_id))

        if project is None:
            await query.edit_message_text(
                text(locale, "project_not_found_md", project_id=escape_markdown(project_id)),
                reply_markup=back_to_menu_keyboard(locale),
                parse_mode="MarkdownV2",
            )
            return

        await query.edit_message_text(
            _format_project_details(project, locale),
            reply_markup=project_details_keyboard(project_id, locale),
            parse_mode="MarkdownV2",
        )

    elif action == ACTION_MAINTENANCE:
        await query.edit_message_text(
            text(locale, "maintenance_md", project_id=escape_markdown(project_id)),
            reply_markup=back_to_menu_keyboard(locale),
            parse_mode="MarkdownV2",
        )

    elif action == ACTION_DEPLOY:
        await query.edit_message_text(
            text(locale, "deploy_md", project_id=escape_markdown(project_id)),
            reply_markup=back_to_menu_keyboard(locale),
            parse_mode="MarkdownV2",
        )


async def _handle_servers(query, parts: list[str], locale: str, user_is_admin: bool) -> None:
    """Handle servers list callbacks.

    Admin-only: regular users cannot access server list.
    """
    # Permission check: only admins can view servers
    if not user_is_admin:
        logger.warning(
            "unauthorized_servers_access",
            telegram_id=query.from_user.id,
            username=query.from_user.username,
        )
        await query.edit_message_text(
            text(locale, "servers_denied_md"),
            reply_markup=back_to_menu_keyboard(locale),
            parse_mode="MarkdownV2",
        )
        return

    action = parts[1] if len(parts) > 1 else ACTION_LIST
    telegram_id = query.from_user.id

    if action == ACTION_LIST:
        servers = await _api_read(api_client.list_managed_servers(telegram_id))

        if servers is None:
            await query.edit_message_text(
                text(locale, "servers_load_failed_md"),
                reply_markup=back_to_menu_keyboard(locale),
                parse_mode="MarkdownV2",
            )
            return

        if not servers:
            await query.edit_message_text(
                text(locale, "servers_empty_md"),
                reply_markup=back_to_menu_keyboard(locale),
                parse_mode="MarkdownV2",
            )
            return

        lines = [text(locale, "servers_title_md"), ""]
        lines.extend(_format_server_line(srv) for srv in servers)

        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=servers_list_keyboard(servers, locale),
            parse_mode="MarkdownV2",
        )


async def _handle_admin(
    query,
    parts: list[str],
    locale: str,
    user_is_admin: bool,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle admin-only callbacks."""
    if not user_is_admin:
        logger.warning(
            "unauthorized_admin_access",
            telegram_id=query.from_user.id,
            username=query.from_user.username,
        )
        await query.edit_message_text(
            text(locale, "admin_denied_md"),
            reply_markup=back_to_menu_keyboard(locale),
            parse_mode="MarkdownV2",
        )
        return

    action = parts[1] if len(parts) > 1 else ""

    if action == ACTION_ADD_USER:
        context.user_data["awaiting_add_user"] = True
        await query.edit_message_text(
            text(locale, "add_user_prompt_md"),
            reply_markup=back_to_menu_keyboard(locale),
            parse_mode="MarkdownV2",
        )


async def cancel_add_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel the pending admin invite flow without consuming the next PO message."""
    locale = user_locale(update)
    if context.user_data.pop("awaiting_add_user", None):
        await update.message.reply_text(text(locale, "add_user_cancelled"))
    else:
        await update.message.reply_text(text(locale, "add_user_not_active"))


async def handle_add_user_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text input when admin is adding a user.

    Returns None if not in add_user flow (so caller can fall through to PO).
    """
    if not context.user_data.get("awaiting_add_user"):
        return None

    locale = user_locale(update)
    entered = update.message.text.strip()

    # Validate numeric telegram_id
    try:
        new_telegram_id = int(entered)
    except ValueError:
        await update.message.reply_text(text(locale, "telegram_id_not_number"))
        return

    # Mint a code; the recipient must register through the canonical promo gate.
    try:
        credits_microusd = await _required_int_config(INVITE_CREDITS_CONFIG_KEY)
        reservation_microusd = await _required_int_config(INVITE_RESERVATION_CONFIG_KEY)
        codes = await api_client.post_json(
            "promo-codes/batch",
            json={
                "quantity": 1,
                "credits_microusd": credits_microusd,
                "attempt_reservation_microusd": reservation_microusd,
            },
        )
        promo_code = codes[0]["code"]
        context.user_data.pop("awaiting_add_user", None)
        await update.message.reply_text(
            text(
                locale,
                "promo_minted_html",
                telegram_id=new_telegram_id,
                code=promo_code,
                credits=format_microusd(credits_microusd),
                reservation=format_microusd(reservation_microusd),
            ),
            parse_mode="HTML",
        )
        logger.info(
            "user_added_by_admin",
            admin_id=update.effective_user.id,
            new_user_telegram_id=new_telegram_id,
            credits_microusd=credits_microusd,
            attempt_reservation_microusd=reservation_microusd,
        )
    except MissingSystemConfigError as e:
        context.user_data.pop("awaiting_add_user", None)
        logger.error("add_user_missing_system_config", key=e.key)
        await update.message.reply_text(text(locale, "missing_system_config", key=e.key))
    except httpx.HTTPStatusError as e:
        context.user_data.pop("awaiting_add_user", None)
        if e.response.status_code == HTTPStatus.BAD_REQUEST:
            await update.message.reply_text(
                text(locale, "user_exists", telegram_id=new_telegram_id)
            )
        else:
            await update.message.reply_text(
                text(locale, "api_error", status=e.response.status_code)
            )
    except httpx.HTTPError as e:
        context.user_data.pop("awaiting_add_user", None)
        logger.error("add_user_api_error", error=str(e))
        await update.message.reply_text(text(locale, "api_unreachable"))
