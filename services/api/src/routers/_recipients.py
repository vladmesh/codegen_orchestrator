"""Resolve the Telegram chat behind a project, for queue messages the API publishes.

``Project.owner_id`` is a ``User.id``; the Telegram transport addresses
``User.telegram_id``. Router endpoints that dispatch pipeline work resolve the
one into the other here, so the lifecycle events that work produces have a
destination instead of an internal id nothing can deliver to.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.models.project import Project
from shared.models.user import User
from shared.notifications import notify_admins_best_effort

logger = structlog.get_logger()


async def resolve_project_chat_id(db: AsyncSession, project_id, *, event: str) -> str:
    """Return the owner's Telegram chat id as a string, "" when unresolvable.

    An unresolvable owner is reported to admins rather than dropped: the work
    still goes out, but somebody has to know its result cannot reach a user.
    """
    result = await db.execute(
        select(User.telegram_id)
        .join(Project, Project.owner_id == User.id)
        .where(Project.id == project_id)
    )
    telegram_id = result.scalar_one_or_none()
    if telegram_id:
        return str(telegram_id)

    logger.error(
        "notification_recipient_unresolved",
        reason="project owner has no telegram id",
        po_event=event,
        project_id=str(project_id),
    )
    await notify_admins_best_effort(
        f"Notification recipient unresolved (project owner has no telegram id): "
        f"event={event} project={project_id}",
        level="error",
        po_event=event,
        project_id=str(project_id),
    )
    return ""
