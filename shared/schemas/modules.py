"""Service modules enum for project scaffolding.

Defines available modules that can be scaffolded from service-template.
"""

from enum import Enum


class ServiceModule(str, Enum):
    """Available modules for project scaffolding.

    These correspond to modules in vladmesh/service-template.
    BACKEND is always required.
    """

    BACKEND = "backend"  # Core backend service (required)
    TG_BOT = "tg_bot"  # Telegram bot service
    NOTIFICATIONS = "notifications"  # Notifications worker
    FRONTEND = "frontend"  # Frontend service
