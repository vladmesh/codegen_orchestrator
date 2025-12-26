"""Callback query handlers for inline keyboard buttons.

Handles direct API calls without going through LangGraph/LLM.
"""

import re

import httpx
import structlog
from telegram import Update
from telegram.ext import ContextTypes

from .config import get_settings
from .keyboards import (
    ACTION_BACK,
    ACTION_DEPLOY,
    ACTION_DETAILS,
    ACTION_LIST,
    ACTION_MAINTENANCE,
    ACTION_NEW,
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

logger = structlog.get_logger()


# ... imports ...


def escape_markdown(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    if not text:
        return ""
    # Characters to escape: _ * [ ] ( ) ~ ` > # + - = | { } . !
    escape_chars = r"_*[]()~`>#+-=|{}!."
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", str(text))


async def _api_get(path: str) -> dict | list | None:
    """Make GET request to API service."""
    settings = get_settings()
    # Ensure path starts with /
    if not path.startswith("/"):
        path = f"/{path}"

    url = f"{settings.api_url}{path}"

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        logger.error("api_request_failed", url=url, error=str(e))
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

    logger.info("callback_received", data=data, prefix=prefix)

    try:
        if prefix == PREFIX_MENU:
            await _handle_menu(query, parts)
        elif prefix == PREFIX_PROJECTS:
            await _handle_projects(query, parts)
        elif prefix == PREFIX_PROJECT:
            await _handle_project(query, parts)
        elif prefix == PREFIX_SERVERS:
            await _handle_servers(query, parts)
        else:
            logger.warning("unknown_callback_prefix", prefix=prefix, data=data)
    except Exception as e:
        logger.error("callback_handler_failed", data=data, error=str(e), exc_info=True)
        await query.edit_message_text(
            f"⚠️ Ошибка: {escape_markdown(str(e))}",
            reply_markup=back_to_menu_keyboard(),
            parse_mode="MarkdownV2",
        )


async def _handle_menu(query, parts: list[str]) -> None:
    """Handle menu callbacks."""
    action = parts[1] if len(parts) > 1 else ACTION_BACK

    if action == ACTION_BACK:
        await query.edit_message_text(
            "🏠 *Главное меню*\n\nВыберите действие:",
            reply_markup=main_menu_keyboard(),
            parse_mode="MarkdownV2",
        )


async def _handle_projects(query, parts: list[str]) -> None:
    """Handle projects list callbacks."""
    action = parts[1] if len(parts) > 1 else ACTION_LIST

    if action == ACTION_LIST:
        # Updated endpoint to /api/projects
        projects = await _api_get("/api/projects")

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
    project_id = parts[2] if len(parts) > 2 else ""  # noqa: PLR2004

    if not project_id:
        await query.edit_message_text(
            "⚠️ ID проекта не указан",
            reply_markup=back_to_menu_keyboard(),
            parse_mode="MarkdownV2",
        )
        return

    if action == ACTION_DETAILS:
        # Updated endpoint to /api/projects
        project = await _api_get(f"/api/projects/{project_id}")

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


async def _handle_servers(query, parts: list[str]) -> None:
    """Handle servers list callbacks."""
    action = parts[1] if len(parts) > 1 else ACTION_LIST

    if action == ACTION_LIST:
        servers = await _api_get("/api/servers?is_managed=true")

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
