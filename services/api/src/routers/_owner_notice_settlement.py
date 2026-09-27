"""Read and settle durable PO notices without changing their delivery lifecycle."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.owner_notification import (
    OWNER_NOTIFICATION_KEY,
    AddressedOwnerNotice,
    OwnerNoticeSettlement,
    OwnerNotification,
    OwnerNotificationState,
)
from shared.models.run import Run
from shared.models.story import Story
from shared.notifications import AdminDeliveryStatus, deliver_to_admins

from ..database import get_async_session
from ..dependencies import require_internal_or_admin
from ..owner_notification_settlement import settle_notice
from ._story_helpers import _get_story, _get_story_for_update

notice_router = APIRouter(dependencies=[Depends(require_internal_or_admin)])
logger = structlog.get_logger(__name__)


def _notices(source: str, source_id: str, stored: dict | None) -> list[AddressedOwnerNotice]:
    if stored is None:
        return []
    root = OwnerNotification.model_validate(stored)
    return [
        AddressedOwnerNotice(
            source=source, source_id=source_id, owed_at=record.owed_at, notification=record
        )
        for record in [root, *root.deferred]
    ]


async def _story_notices(story: Story, db: AsyncSession, *, lock: bool = False):
    query = select(Run).where(Run.story_id == story.id).order_by(Run.id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    runs = (await db.execute(query)).scalars().all()
    notices = _notices("story", story.id, story.owner_notification)
    for run in runs:
        notices.extend(
            _notices("run", run.id, (run.run_metadata or {}).get(OWNER_NOTIFICATION_KEY))
        )
    return sorted(notices, key=lambda item: item.owed_at, reverse=True), runs


@notice_router.get("/owner-notifications/deferred", response_model=list[AddressedOwnerNotice])
async def deferred_notices(project_id: uuid.UUID, db: AsyncSession = Depends(get_async_session)):
    stories = (
        (await db.execute(select(Story).where(Story.project_id == project_id))).scalars().all()
    )
    result = []
    for story in stories:
        notices, _ = await _story_notices(story, db)
        result.extend(n for n in notices if n.notification.told_state == "suppressed")
    return sorted(result, key=lambda item: item.owed_at)


@notice_router.get("/{story_id}/owner-notifications", response_model=list[AddressedOwnerNotice])
async def story_notices(story_id: str, db: AsyncSession = Depends(get_async_session)):
    notices, _ = await _story_notices(await _get_story(story_id, db), db)
    return notices


def _replace(
    story: Story, runs: list[Run], command: OwnerNoticeSettlement, settled: OwnerNotification
) -> None:
    if command.source == "story":
        row = story
        stored = story.owner_notification
    else:
        row = next(run for run in runs if run.id == command.source_id)
        stored = row.run_metadata[OWNER_NOTIFICATION_KEY]
    root = OwnerNotification.model_validate(stored)
    if root.owed_at == command.owed_at:
        root = settled
    else:
        root = root.model_copy(
            update={
                "deferred": [
                    settled if item.owed_at == command.owed_at else item for item in root.deferred
                ]
            }
        )
    if command.source == "story":
        row.owner_notification = root.model_dump(mode="json")
    else:
        row.run_metadata = {
            **row.run_metadata,
            OWNER_NOTIFICATION_KEY: root.model_dump(mode="json"),
        }


@notice_router.post("/{story_id}/owner-notifications/settlement", response_model=OwnerNotification)
async def settle_owner_notice(
    story_id: str, command: OwnerNoticeSettlement, db: AsyncSession = Depends(get_async_session)
):
    story = await _get_story_for_update(story_id, db)
    notices, runs = await _story_notices(story, db, lock=True)
    target = next(
        (
            n
            for n in notices
            if n.source == command.source
            and n.source_id == command.source_id
            and n.owed_at == command.owed_at
        ),
        None,
    )
    if target is None:
        raise HTTPException(409, "Owner notice was replaced; read the current notice")
    if command.told_state == "suppressed":
        delivered = [n for n in notices if n.notification.state == OwnerNotificationState.DELIVERED]
        if not delivered or delivered[0] != target:
            raise HTTPException(409, "Only the latest delivered notice can be deferred")
    current = next(
        n for n in notices if n.source == command.source and n.source_id == command.source_id
    )
    settled = settle_notice(target.notification, command, current_owed_at=current.owed_at)
    if command.told_state == "suppressed":
        copy = (
            f"Deferred owner notice: story={story_id} event={settled.event}\n"
            f"{settled.text}\nReason: {settled.suppressed_reason}\n"
            f"Decided by: {settled.suppressed_by}"
        )
        if settled.admin_owed:
            copy = f"{settled.admin_text}\n\n{copy}"
        settled = settled.model_copy(
            update={
                "admin_text": copy,
                "admin_state": OwnerNotificationState.OWED,
                "admin_attempts": 0,
                "admin_detail": None,
            }
        )
    _replace(story, runs, command, settled)
    await db.commit()
    if command.told_state == "suppressed":
        # The existing administrator audience recovers a failed immediate copy.
        try:
            result = await deliver_to_admins(settled.admin_text, level="info")
        except Exception as exc:
            logger.warning("owner_notice_admin_copy_owed", story_id=story_id, error=str(exc))
            return settled
        if result.status == AdminDeliveryStatus.DELIVERED:
            story = await _get_story_for_update(story_id, db)
            await db.refresh(story)
            notices, runs = await _story_notices(story, db, lock=True)
            current = next(
                n.notification
                for n in notices
                if n.source == command.source
                and n.source_id == command.source_id
                and n.owed_at == command.owed_at
            )
            if current.admin_text == settled.admin_text:
                current = current.model_copy(
                    update={"admin_state": OwnerNotificationState.DELIVERED}
                )
                _replace(story, runs, command, current)
                await db.commit()
    return settled
