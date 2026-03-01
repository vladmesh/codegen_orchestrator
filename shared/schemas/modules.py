"""Service modules enum for project scaffolding.

Defines available modules that can be scaffolded from service-template.
"""

from enum import Enum


class ServiceModule(str, Enum):
    """Available modules for project scaffolding.

    These correspond to modules in vladmesh/service-template.
    Any combination is valid (at least one required).
    """

    BACKEND = "backend"  # FastAPI REST API + PostgreSQL
    TG_BOT = "tg_bot"  # Telegram bot service
    NOTIFICATIONS = "notifications"  # Notifications worker (requires backend)
    FRONTEND = "frontend"  # Frontend service
