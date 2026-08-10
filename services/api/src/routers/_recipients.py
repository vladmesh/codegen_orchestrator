"""Resolve the Telegram chat behind a project, for queue messages the API publishes.

``Project.owner_id`` is a ``User.id``; the Telegram transport addresses
``User.telegram_id``. Router endpoints that dispatch pipeline work resolve the
one into the other here, so the lifecycle events that work produces have a
destination instead of an internal id nothing can deliver to.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.models.project import Project
from shared.models.user import User
from shared.notifications import notify_admins_best_effort

logger = structlog.get_logger()

# What a message says when its recipient could not be resolved. Contracts that
# demand a recipient or a reason (``DeployMessage``) carry this, so an
# unresolvable owner is not indistinguishable from a producer that forgot to
# resolve one.
UNRESOLVED_REASON = "recipient unresolved: project owner has no telegram id"


@dataclass(frozen=True)
class ProjectRecipient:
    """Where a project's pipeline event goes, or why it goes nowhere."""

    telegram_chat_id: str = ""
    unaddressed_reason: str = ""


async def resolve_project_recipient(
    db: AsyncSession, project_id, *, event: str, story_id: str = ""
) -> ProjectRecipient:
    """Resolve the owner's chat, or the reason there is none, for *project_id*."""
    chat_id = await resolve_project_chat_id(db, project_id, event=event, story_id=story_id)
    if chat_id:
        return ProjectRecipient(telegram_chat_id=chat_id)
    return ProjectRecipient(unaddressed_reason=UNRESOLVED_REASON)


async def resolve_project_chat_id(
    db: AsyncSession, project_id, *, event: str, story_id: str = ""
) -> str:
    """Return the owner's Telegram chat id as a string, "" when unresolvable.

    An unresolvable owner is reported to admins rather than dropped: the work
    still goes out, but somebody has to know its result cannot reach a user. The
    alert names the story as well as the project and the event, so pass
    ``story_id`` wherever the caller holds one — that is the identifier somebody
    reading the alert starts from.
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
        story_id=story_id,
        project_id=str(project_id),
    )
    await notify_admins_best_effort(
        f"Notification recipient unresolved (project owner has no telegram id): "
        f"event={event} story={story_id or '-'} project={project_id}",
        level="error",
        po_event=event,
        story_id=story_id,
        project_id=str(project_id),
    )
    return ""
