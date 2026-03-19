"""Callback query handlers for inline keyboard buttons.

Handles direct API calls without going through LangGraph/LLM.
"""

from http import HTTPStatus
import re

import httpx
import structlog
from telegram import Update
from telegram.ext import ContextTypes

from .clients.api import api_client
from .keyboards import (
    ACTION_ADD_USER,
    ACTION_BACK,
    ACTION_DEPLOY,
    ACTION_DETAILS,
    ACTION_LIST,
    ACTION_MAINTENANCE,
    ACTION_NEW,
    PREFIX_ADMIN,
    PREFIX_MENU,
    PREFIX_PROJECT,
    PREFIX_PROJECTS,
    PREFIX_SERVERS,
    back_to_menu_keyboard,
    main_menu_keyboard,
    project_details_keyboard,
    projects_list_keyboard,
    servers_list_keyboard,
)
from .middleware import is_admin

logger = structlog.get_logger()


# ... imports ...


def escape_markdown(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    if not text:
        return ""
    # Characters to escape: _ * [ ] ( ) ~ ` > # + - = | { } . !
    escape_chars = r"_*[]()~`>#+-=|{}!."
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", str(text))


async def _api_get(path: str, telegram_id: int | None = None) -> dict | list | None:
    """Make GET request to API service."""
    headers = {}
    if telegram_id:
        headers["X-Telegram-ID"] = str(telegram_id)

    try:
        return await api_client.get_json(path, headers=headers)
    except httpx.HTTPError as e:
        logger.error("api_request_failed", path=path, error=str(e))
        return None


def _format_project_details(project: dict) -> str:
    """Format project details for display."""
    name = project.get("name", "Unknown")
    status = project.get("status", "unknown")
    project_id = project.get("id", "")

    config = project.get("config") or {}
    description = config.get("description", "Нет описания")

    repo_url = config.get("repo_url", "")
    deployed_url = config.get("deployed_url", "")

    status_emoji = "✅" if status == "active" else "📝" if status == "draft" else "⚪"

    # Escape all dynamic values
    name_esc = escape_markdown(name)
    id_esc = escape_markdown(project_id)
    status_esc = escape_markdown(status)
    desc_esc = escape_markdown(description)

    lines = [
        f"📦 *{name_esc}* {status_emoji}",
        f"ID: `{id_esc}`",
        f"Статус: {status_esc}",
        "",
        f"📝 {desc_esc}",
    ]

    if repo_url:
        lines.append(f"🔗 Репо: {escape_markdown(repo_url)}")
    if deployed_url:
        lines.append(f"🌐 URL: {escape_markdown(deployed_url)}")

    return "\n".join(lines)


def _format_server_line(server: dict) -> str:
    """Format single server line."""
    handle = server.get("handle", "unknown")
    status = server.get("status", "unknown")
    ip = server.get("public_ip", "")
    ram_total = server.get("capacity_ram_mb", 0)
    ram_used = server.get("used_ram_mb", 0)

    status_emoji = "✅" if status in ("ready", "in_use") else "⚠️"

    handle_esc = escape_markdown(handle)
    status_esc = escape_markdown(status)
    ip_esc = escape_markdown(ip)

    return (
        f"{status_emoji} *{handle_esc}* \\[{status_esc}\\] — {ip_esc} "
        f"\\(RAM: {ram_used}/{ram_total} MB\\)"
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

    logger.info("callback_received", data=data, prefix=prefix, is_admin=user_is_admin)

    try:
        if prefix == PREFIX_MENU:
            await _handle_menu(query, parts, user_is_admin)
        elif prefix == PREFIX_PROJECTS:
            await _handle_projects(query, parts)
        elif prefix == PREFIX_PROJECT:
            await _handle_project(query, parts)
        elif prefix == PREFIX_SERVERS:
            await _handle_servers(query, parts, user_is_admin)
        elif prefix == PREFIX_ADMIN:
            await _handle_admin(query, parts, user_is_admin, context)
        else:
            logger.warning("unknown_callback_prefix", prefix=prefix, data=data)
    except Exception as e:
        logger.error("callback_handler_failed", data=data, error=str(e), exc_info=True)
        await query.edit_message_text(
            f"⚠️ Ошибка: {escape_markdown(str(e))}",
            reply_markup=back_to_menu_keyboard(),
            parse_mode="MarkdownV2",
        )


async def _handle_menu(query, parts: list[str], user_is_admin: bool = False) -> None:
    """Handle menu callbacks."""
    action = parts[1] if len(parts) > 1 else ACTION_BACK

    if action == ACTION_BACK:
        await query.edit_message_text(
            "🏠 *Главное меню*\n\nВыберите действие:",
            reply_markup=main_menu_keyboard(is_admin=user_is_admin),
            parse_mode="MarkdownV2",
        )


async def _handle_projects(query, parts: list[str]) -> None:
    """Handle projects list callbacks."""
    action = parts[1] if len(parts) > 1 else ACTION_LIST
    telegram_id = query.from_user.id

    if action == ACTION_LIST:
        # Updated endpoint to /projects
        projects = await _api_get("/projects", telegram_id=telegram_id)

        if projects is None:
            await query.edit_message_text(
                "⚠️ Не удалось загрузить проекты",
                reply_markup=back_to_menu_keyboard(),
                parse_mode="MarkdownV2",
            )
            return

        if not projects:
            await query.edit_message_text(
                "📦 Проектов пока нет\\.\n\nСоздайте новый проект или опишите идею в чате\\.",
                reply_markup=back_to_menu_keyboard(),
                parse_mode="MarkdownV2",
            )
            return

        await query.edit_message_text(
            "📦 *Проекты:*\n\nНажмите на проект для подробностей:",
            reply_markup=projects_list_keyboard(projects),
            parse_mode="MarkdownV2",
        )

    elif action == ACTION_NEW:
        await query.edit_message_text(
            "➕ *Новый проект*\n\n"
            "Опишите ваш проект в чате\\. Например:\n\n"
            "_Создай телеграм\\-бота для погоды с командами /weather и /settings_",
            reply_markup=back_to_menu_keyboard(),
            parse_mode="MarkdownV2",
        )


async def _handle_project(query, parts: list[str]) -> None:
    """Handle single project callbacks."""
    action = parts[1] if len(parts) > 1 else ""
    project_id = parts[2] if len(parts) > 2 else ""  # noqa: PLR2004 — index into callback_data parts
    telegram_id = query.from_user.id

    if not project_id:
        await query.edit_message_text(
            "⚠️ ID проекта не указан",
            reply_markup=back_to_menu_keyboard(),
            parse_mode="MarkdownV2",
        )
        return

    if action == ACTION_DETAILS:
        # Updated endpoint to /projects
        project = await _api_get(f"/projects/{project_id}", telegram_id=telegram_id)

        if project is None:
            await query.edit_message_text(
                f"⚠️ Проект {escape_markdown(project_id)} не найден",
                reply_markup=back_to_menu_keyboard(),
                parse_mode="MarkdownV2",
            )
            return

        await query.edit_message_text(
            _format_project_details(project),
            reply_markup=project_details_keyboard(project_id),
            parse_mode="MarkdownV2",
        )

    elif action == ACTION_MAINTENANCE:
        await query.edit_message_text(
            f"🔧 *Режим обслуживания*\n\n"
            f"Опишите, что нужно изменить в проекте `{escape_markdown(project_id)}`:",
            reply_markup=back_to_menu_keyboard(),
            parse_mode="MarkdownV2",
        )

    elif action == ACTION_DEPLOY:
        pid_esc = escape_markdown(project_id)
        await query.edit_message_text(
            f"🚀 *Деплой проекта*\n\n"
            f"Для запуска деплоя проекта `{pid_esc}` напишите:\n"
            f"_Задеплой проект {pid_esc}_",
            reply_markup=back_to_menu_keyboard(),
            parse_mode="MarkdownV2",
        )


async def _handle_servers(query, parts: list[str], user_is_admin: bool = False) -> None:
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
            "🚫 *Доступ запрещён*\n\nПросмотр серверов доступен только администраторам\\.",
            reply_markup=back_to_menu_keyboard(),
            parse_mode="MarkdownV2",
        )
        return

    action = parts[1] if len(parts) > 1 else ACTION_LIST
    telegram_id = query.from_user.id

    if action == ACTION_LIST:
        servers = await _api_get("/servers?is_managed=true", telegram_id=telegram_id)

        if servers is None:
            await query.edit_message_text(
                "⚠️ Не удалось загрузить серверы",
                reply_markup=back_to_menu_keyboard(),
                parse_mode="MarkdownV2",
            )
            return

        if not servers:
            await query.edit_message_text(
                "🖥️ Активных серверов нет\\.",
                reply_markup=back_to_menu_keyboard(),
                parse_mode="MarkdownV2",
            )
            return

        lines = ["🖥️ *Серверы:*", ""]
        lines.extend(_format_server_line(srv) for srv in servers)

        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=servers_list_keyboard(servers),
            parse_mode="MarkdownV2",
        )


async def _handle_admin(
    query, parts: list[str], user_is_admin: bool, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle admin-only callbacks."""
    if not user_is_admin:
        logger.warning(
            "unauthorized_admin_access",
            telegram_id=query.from_user.id,
            username=query.from_user.username,
        )
        await query.edit_message_text(
            "🚫 *Доступ запрещён*\n\nДоступно только администраторам\\.",
            reply_markup=back_to_menu_keyboard(),
            parse_mode="MarkdownV2",
        )
        return

    action = parts[1] if len(parts) > 1 else ""

    if action == ACTION_ADD_USER:
        context.user_data["awaiting_add_user"] = True
        await query.edit_message_text(
            "👤 *Добавить пользователя*\n\n"
            "Введите Telegram ID нового пользователя\\.\n"
            "Отправьте /cancel для отмены\\.",
            reply_markup=back_to_menu_keyboard(),
            parse_mode="MarkdownV2",
        )


async def handle_add_user_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text input when admin is adding a user.

    Returns None if not in add_user flow (so caller can fall through to PO).
    """
    if not context.user_data.get("awaiting_add_user"):
        return None

    text = update.message.text.strip()

    # Validate numeric telegram_id
    try:
        new_telegram_id = int(text)
    except ValueError:
        await update.message.reply_text(
            "⚠️ Telegram ID должен быть числом. Попробуйте ещё раз или /cancel."
        )
        return

    # Call API to create user
    try:
        await api_client.post_json(
            "users/",
            json={"telegram_id": new_telegram_id},
        )
        context.user_data.pop("awaiting_add_user", None)
        await update.message.reply_text(f"✅ Пользователь {new_telegram_id} добавлен.")
        logger.info(
            "user_added_by_admin",
            admin_id=update.effective_user.id,
            new_user_telegram_id=new_telegram_id,
        )
    except httpx.HTTPStatusError as e:
        context.user_data.pop("awaiting_add_user", None)
        if e.response.status_code == HTTPStatus.BAD_REQUEST:
            await update.message.reply_text(f"⚠️ Пользователь {new_telegram_id} уже существует.")
        else:
            await update.message.reply_text(f"⚠️ Ошибка API: {e.response.status_code}")
    except httpx.HTTPError as e:
        context.user_data.pop("awaiting_add_user", None)
        logger.error("add_user_api_error", error=str(e))
        await update.message.reply_text("⚠️ Ошибка соединения с API.")
