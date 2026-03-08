"""Stories router — CRUD + action-based status transitions."""

from datetime import UTC, datetime
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.story import (
    VALID_TRANSITIONS,
    StoryStatus,
)
from shared.models.story import Story

from ..database import get_async_session
from ..schemas.story import (
    StoryCreate,
    StoryRead,
    StoryTransition,
    StoryUpdate,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/stories", tags=["stories"])


def _generate_id() -> str:
    return f"story-{secrets.token_hex(4)}"


async def _get_story(story_id: str, db: AsyncSession) -> Story:
    query = select(Story).where(Story.id == story_id)
    result = await db.execute(query)
    story = result.scalar_one_or_none()
    if not story:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Story {story_id} not found",
        )
    return story


def _validate_transition(from_status: str, to_status: str) -> None:
    try:
        from_s = StoryStatus(from_status)
    except ValueError:
        raise HTTPException(  # noqa: B904
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid status: {from_status}",
        )
    try:
        to_s = StoryStatus(to_status)
    except ValueError:
        raise HTTPException(  # noqa: B904
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid status: {to_status}",
        )
    if to_s not in VALID_TRANSITIONS[from_s]:
        allowed = [s.value for s in VALID_TRANSITIONS[from_s]]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot transition from {from_status} to {to_status}. Allowed: {allowed}",
        )


# --- CRUD ---


@router.post("/", response_model=StoryRead, status_code=status.HTTP_201_CREATED)
async def create_story(
    body: StoryCreate,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    now = datetime.now(UTC)
    story = Story(
        id=_generate_id(),
        project_id=body.project_id,
        parent_story_id=body.parent_story_id,
        title=body.title,
        description=body.description,
        acceptance_criteria=body.acceptance_criteria,
        status=StoryStatus.CREATED.value,
        priority=body.priority,
        blocked_by_story_id=body.blocked_by_story_id,
        created_by=body.created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(story)
    await db.commit()
    await db.refresh(story)

    logger.info("story_created", story_id=story.id, title=story.title)
    return StoryRead.model_validate(story, from_attributes=True)


@router.get("/", response_model=list[StoryRead])
async def list_stories(
    project_id: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    parent_story_id: str | None = Query(None),
    priority: int | None = Query(None),
    sort: str | None = Query(None),
    db: AsyncSession = Depends(get_async_session),
) -> list[StoryRead]:
    query = select(Story)

    if project_id:
        query = query.where(Story.project_id == project_id)
    if status_filter:
        query = query.where(Story.status == status_filter)
    if parent_story_id:
        query = query.where(Story.parent_story_id == parent_story_id)
    if priority is not None:
        query = query.where(Story.priority == priority)

    if sort == "-created_at":
        query = query.order_by(Story.created_at.desc())
    elif sort == "created_at":
        query = query.order_by(Story.created_at.asc())
    elif sort == "-priority":
        query = query.order_by(Story.priority.desc(), Story.created_at.asc())
    else:
        query = query.order_by(Story.priority.asc(), Story.created_at.asc())

    result = await db.execute(query)
    items = result.scalars().all()
    return [StoryRead.model_validate(s, from_attributes=True) for s in items]


@router.get("/{story_id}", response_model=StoryRead)
async def get_story(
    story_id: str,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    story = await _get_story(story_id, db)
    return StoryRead.model_validate(story, from_attributes=True)


@router.patch("/{story_id}", response_model=StoryRead)
async def update_story(
    story_id: str,
    body: StoryUpdate,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    story = await _get_story(story_id, db)

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(story, field, value)

    await db.commit()
    await db.refresh(story)

    logger.info("story_updated", story_id=story.id, fields=list(update_data.keys()))
    return StoryRead.model_validate(story, from_attributes=True)


# --- Action endpoints (state machine transitions) ---


def _do_transition(story: Story, to_status: StoryStatus) -> None:
    _validate_transition(story.status, to_status.value)
    story.status = to_status.value


@router.post("/{story_id}/start", response_model=StoryRead)
async def start_story(
    story_id: str,
    body: StoryTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    body = body or StoryTransition()
    story = await _get_story(story_id, db)

    if story.blocked_by_story_id:
        blocker = await _get_story(story.blocked_by_story_id, db)
        if blocker.status != StoryStatus.COMPLETED.value:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"Cannot start: blocked by story {blocker.id} "
                    f"(status: {blocker.status}, must be completed)"
                ),
            )

    _do_transition(story, StoryStatus.IN_PROGRESS)
    await db.commit()
    await db.refresh(story)

    logger.info("story_started", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)


@router.post("/{story_id}/complete", response_model=StoryRead)
async def complete_story(
    story_id: str,
    body: StoryTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    body = body or StoryTransition()
    story = await _get_story(story_id, db)

    _do_transition(story, StoryStatus.COMPLETED)
    await db.commit()
    await db.refresh(story)

    logger.info("story_completed", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)


@router.post("/{story_id}/archive", response_model=StoryRead)
async def archive_story(
    story_id: str,
    body: StoryTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    body = body or StoryTransition()
    story = await _get_story(story_id, db)

    _do_transition(story, StoryStatus.ARCHIVED)
    await db.commit()
    await db.refresh(story)

    logger.info("story_archived", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)
