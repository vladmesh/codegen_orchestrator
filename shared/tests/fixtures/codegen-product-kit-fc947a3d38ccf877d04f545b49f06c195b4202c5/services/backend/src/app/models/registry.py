"""Explicit registration of user-owned ORM models for Alembic metadata."""

from .event_consumption import EventConsumption  # noqa: F401
from .job_command import JobCommand  # noqa: F401
from .setting import Setting  # noqa: F401
from .user import User, UserChannel  # noqa: F401
