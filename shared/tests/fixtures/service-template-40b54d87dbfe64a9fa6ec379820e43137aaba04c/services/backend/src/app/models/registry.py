"""Explicit registration of user-owned ORM models for Alembic metadata."""

from .job_command import JobCommand  # noqa: F401
from .setting import Setting  # noqa: F401
from .user import User, UserChannel  # noqa: F401
