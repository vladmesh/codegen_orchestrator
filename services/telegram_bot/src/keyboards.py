"""Telegram keyboard definitions for inline buttons."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Callback data prefixes
PREFIX_MENU = "menu"
PREFIX_PROJECTS = "projects"
PREFIX_SERVERS = "servers"
PREFIX_PROJECT = "project"

# Callback actions
ACTION_LIST = "list"
ACTION_DETAILS = "details"
ACTION_NEW = "new"
ACTION_BACK = "back"
ACTION_DEPLOY = "deploy"
ACTION_MAINTENANCE = "maintenance"
ACTION_ADD_USER = "add_user"

# Admin prefix
PREFIX_ADMIN = "admin"


def main_menu_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    """Build main menu keyboard.

    Args:
        is_admin: If True, show admin-only options (servers list).
    """
    keyboard = [
        [
            InlineKeyboardButton(
                "📦 Список проектов", callback_data=f"{PREFIX_PROJECTS}:{ACTION_LIST}"
            )
        ],
        [InlineKeyboardButton("➕ Новый проект", callback_data=f"{PREFIX_PROJECTS}:{ACTION_NEW}")],
    ]

    # Admin-only buttons
    if is_admin:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "🖥️ Список серверов", callback_data=f"{PREFIX_SERVERS}:{ACTION_LIST}"
                )
            ]
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    "👤 Добавить пользователя",
                    callback_data=f"{PREFIX_ADMIN}:{ACTION_ADD_USER}",
                )
            ]
        )

    return InlineKeyboardMarkup(keyboard)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    """Build back to main menu keyboard."""
    keyboard = [
        [InlineKeyboardButton("← Главное меню", callback_data=f"{PREFIX_MENU}:{ACTION_BACK}")],
    ]
    return InlineKeyboardMarkup(keyboard)


def projects_list_keyboard(projects: list[dict]) -> InlineKeyboardMarkup:
    """Build projects list keyboard with details buttons.

    Args:
        projects: List of project dicts with 'id' and 'name' keys.
    """
    keyboard = []

    for project in projects[:10]:  # Limit to 10 projects
        project_id = project.get("id", "")
        name = project.get("name", "Unknown")
        status = project.get("status", "")
        status_emoji = "✅" if status == "active" else "📝" if status == "draft" else "⚪"

        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{status_emoji} {name}",
                    callback_data=f"{PREFIX_PROJECT}:{ACTION_DETAILS}:{project_id}",
                )
            ]
        )

    keyboard.append(
        [InlineKeyboardButton("← Главное меню", callback_data=f"{PREFIX_MENU}:{ACTION_BACK}")]
    )

    return InlineKeyboardMarkup(keyboard)


def project_details_keyboard(project_id: str) -> InlineKeyboardMarkup:
    """Build project details keyboard with action buttons."""
    keyboard = [
        [
            InlineKeyboardButton(
                "🔧 Maintenance",
                callback_data=f"{PREFIX_PROJECT}:{ACTION_MAINTENANCE}:{project_id}",
            ),
            InlineKeyboardButton(
                "🚀 Deploy", callback_data=f"{PREFIX_PROJECT}:{ACTION_DEPLOY}:{project_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                "← Список проектов", callback_data=f"{PREFIX_PROJECTS}:{ACTION_LIST}"
            )
        ],
        [InlineKeyboardButton("← Главное меню", callback_data=f"{PREFIX_MENU}:{ACTION_BACK}")],
    ]
    return InlineKeyboardMarkup(keyboard)


def servers_list_keyboard(servers: list[dict]) -> InlineKeyboardMarkup:
    """Build servers list with back button.

    Args:
        servers: List of server dicts.
    """
    # Servers displayed as text, only back button needed
    keyboard = [
        [InlineKeyboardButton("← Главное меню", callback_data=f"{PREFIX_MENU}:{ACTION_BACK}")],
    ]
    return InlineKeyboardMarkup(keyboard)
