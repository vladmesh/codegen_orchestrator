"""Telegram keyboard definitions for inline buttons."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .texts import text

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

# Dashboard (all users)
PREFIX_DASHBOARD = "dashboard"

# Admin prefix
PREFIX_ADMIN = "admin"


def main_menu_button(locale: str) -> InlineKeyboardButton:
    """The button that returns to the main menu."""
    return InlineKeyboardButton(
        text(locale, "btn_main_menu"), callback_data=f"{PREFIX_MENU}:{ACTION_BACK}"
    )


def main_menu_keyboard(locale: str, is_admin: bool = False) -> InlineKeyboardMarkup:
    """Build main menu keyboard.

    Args:
        locale: The user's locale (`texts.user_locale`).
        is_admin: If True, show admin-only options (servers list).
    """
    keyboard = [
        [
            InlineKeyboardButton(
                text(locale, "btn_projects_list"),
                callback_data=f"{PREFIX_PROJECTS}:{ACTION_LIST}",
            )
        ],
        [
            InlineKeyboardButton(
                text(locale, "btn_new_project"),
                callback_data=f"{PREFIX_PROJECTS}:{ACTION_NEW}",
            )
        ],
        [InlineKeyboardButton(text(locale, "btn_my_dashboard"), callback_data=PREFIX_DASHBOARD)],
    ]

    # Admin-only buttons
    if is_admin:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text(locale, "btn_servers_list"),
                    callback_data=f"{PREFIX_SERVERS}:{ACTION_LIST}",
                )
            ]
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    text(locale, "btn_add_user"),
                    callback_data=f"{PREFIX_ADMIN}:{ACTION_ADD_USER}",
                )
            ]
        )

    return InlineKeyboardMarkup(keyboard)


def back_to_menu_keyboard(locale: str) -> InlineKeyboardMarkup:
    """Build back to main menu keyboard."""
    return InlineKeyboardMarkup([[main_menu_button(locale)]])


def projects_list_keyboard(projects: list[dict], locale: str) -> InlineKeyboardMarkup:
    """Build projects list keyboard with details buttons.

    Args:
        projects: List of project dicts with 'id' and 'title' keys.
        locale: The user's locale (`texts.user_locale`).
    """
    keyboard = []

    for project in projects[:10]:  # Limit to 10 projects
        project_id = project.get("id", "")
        name = project.get("title", "Unknown")
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

    keyboard.append([main_menu_button(locale)])

    return InlineKeyboardMarkup(keyboard)


def project_details_keyboard(project_id: str, locale: str) -> InlineKeyboardMarkup:
    """Build project details keyboard with action buttons."""
    keyboard = [
        [
            InlineKeyboardButton(
                text(locale, "btn_maintenance"),
                callback_data=f"{PREFIX_PROJECT}:{ACTION_MAINTENANCE}:{project_id}",
            ),
            InlineKeyboardButton(
                text(locale, "btn_deploy"),
                callback_data=f"{PREFIX_PROJECT}:{ACTION_DEPLOY}:{project_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text(locale, "btn_back_to_projects"),
                callback_data=f"{PREFIX_PROJECTS}:{ACTION_LIST}",
            )
        ],
        [main_menu_button(locale)],
    ]
    return InlineKeyboardMarkup(keyboard)


def servers_list_keyboard(servers: list[dict], locale: str) -> InlineKeyboardMarkup:
    """Build servers list with back button.

    Args:
        servers: List of server dicts.
        locale: The user's locale (`texts.user_locale`).
    """
    # Servers displayed as text, only back button needed
    return back_to_menu_keyboard(locale)
