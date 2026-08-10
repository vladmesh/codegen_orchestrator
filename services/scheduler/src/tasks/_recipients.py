"""Resolve the Telegram chat a pipeline-born notification has to reach.

The scheduler only ever holds internal identifiers — ``Project.owner_id`` is a
``User.id``, not a Telegram chat id. Publishing that number as a recipient sends
the message to a chat that does not exist, so every producer resolves it here
*before* it publishes, and a recipient that cannot be resolved raises an admin
alert instead of disappearing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from shared.notifications import notify_admins_best_effort

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Recipient:
    """Who a pipeline event is delivered to.

    ``telegram_chat_id`` empty means the event has no reachable user; the
    producer has already alerted admins and must publish the message without a
    destination rather than inventing one.
    """

    telegram_chat_id: str = ""
    owner_user_id: str = ""

    @property
    def is_addressable(self) -> bool:
        return bool(self.telegram_chat_id)


async def _alert_unresolved(
    reason: str,
    *,
    event: str,
    project_id: str,
    story_id: str,
    owner_user_id: str,
) -> None:
    logger.error(
        "notification_recipient_unresolved",
        reason=reason,
        po_event=event,
        project_id=project_id,
        story_id=story_id,
        owner_user_id=owner_user_id,
    )
    await notify_admins_best_effort(
        f"Notification recipient unresolved ({reason}): event={event or '-'} "
        f"story={story_id or '-'} project={project_id or '-'} "
        f"owner_user_id={owner_user_id or '-'}",
        level="error",
        po_event=event,
        project_id=project_id,
        story_id=story_id,
    )


async def resolve_owner_recipient(
    api_client: SchedulerAPIClient,
    owner_id: int | str | None,
    *,
    event: str,
    project_id: str = "",
    story_id: str = "",
) -> Recipient:
    """Turn an internal ``User.id`` into the Telegram chat that user reads."""
    if not owner_id:
        await _alert_unresolved(
            "project has no owner",
            event=event,
            project_id=project_id,
            story_id=story_id,
            owner_user_id="",
        )
        return Recipient()

    user = await api_client.get_user(int(owner_id))
    if user is None or not user.telegram_id:
        await _alert_unresolved(
            "owner has no telegram id" if user is not None else "owner user not found",
            event=event,
            project_id=project_id,
            story_id=story_id,
            owner_user_id=str(owner_id),
        )
        return Recipient(owner_user_id=str(owner_id))

    return Recipient(telegram_chat_id=str(user.telegram_id), owner_user_id=str(owner_id))


async def resolve_project_recipient(
    api_client: SchedulerAPIClient,
    project_id: str,
    *,
    event: str,
    story_id: str = "",
) -> Recipient:
    """Resolve the owner of *project_id* down to a Telegram chat id."""
    project = await api_client.get_project(str(project_id))
    if project is None:
        await _alert_unresolved(
            "project not found",
            event=event,
            project_id=str(project_id),
            story_id=story_id,
            owner_user_id="",
        )
        return Recipient()
    return await resolve_owner_recipient(
        api_client,
        project.owner_id,
        event=event,
        project_id=str(project_id),
        story_id=story_id,
    )
