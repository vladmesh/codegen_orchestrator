"""Stories router — CRUD + action-based status transitions."""

from datetime import UTC, datetime
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.story import (
    VALID_TRANSITIONS,
    StoryStatus,
)
from shared.contracts.queues.architect import ArchitectMessage
from shared.models.story import Story
from shared.queues import ARCHITECT_QUEUE
from shared.redis.client import RedisStreamClient

from ..database import get_async_session
from ..dependencies import get_redis_client
from ..schemas.actions import AdminAction
from ..schemas.story import (
    StoryCreate,
    StoryRead,
    StoryReopen,
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
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid status: {from_status}",
        ) from e
    try:
        to_s = StoryStatus(to_status)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid status: {to_status}",
        ) from e
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
        type=body.type,
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
    project_id: uuid.UUID | None = None,
    status_filter: str | None = Query(None, alias="status"),
    parent_story_id: str | None = Query(None),
    type_filter: str | None = Query(None, alias="type"),
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
    if type_filter:
        query = query.where(Story.type == type_filter)
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


@router.post("/{story_id}/human-review", response_model=StoryRead)
async def human_review_story(
    story_id: str,
    body: StoryTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    """Move a blocked active story to the visible human-review queue."""
    body = body or StoryTransition()
    story = await _get_story(story_id, db)
    _do_transition(story, StoryStatus.WAITING_HUMAN_REVIEW)
    await db.commit()
    await db.refresh(story)
    logger.info("story_waiting_human_review", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)


@router.post("/{story_id}/wait-user-secret", response_model=StoryRead)
async def wait_user_secret_story(
    story_id: str,
    body: StoryTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    """Park a deploying story that needs a user secret it does not have yet.

    The scheduler re-dispatches the deploy (→ DEPLOYING) once the secret appears,
    so this is a non-terminal wait, not a failure.
    """
    body = body or StoryTransition()
    story = await _get_story(story_id, db)
    _do_transition(story, StoryStatus.WAITING_USER_SECRET)
    await db.commit()
    await db.refresh(story)
    logger.info("story_waiting_user_secret", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)


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


@router.post("/{story_id}/fail", response_model=StoryRead)
async def fail_story(
    story_id: str,
    body: StoryTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    body = body or StoryTransition()
    story = await _get_story(story_id, db)

    _do_transition(story, StoryStatus.FAILED)
    await db.commit()
    await db.refresh(story)

    logger.info("story_failed", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)


@router.post("/{story_id}/pr_review", response_model=StoryRead)
async def pr_review_story(
    story_id: str,
    body: StoryTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    body = body or StoryTransition()
    story = await _get_story(story_id, db)

    _do_transition(story, StoryStatus.PR_REVIEW)
    await db.commit()
    await db.refresh(story)

    logger.info("story_pr_review", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)


@router.post("/{story_id}/deploy", response_model=StoryRead)
async def deploy_story(
    story_id: str,
    body: StoryTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    body = body or StoryTransition()
    story = await _get_story(story_id, db)

    _do_transition(story, StoryStatus.DEPLOYING)
    await db.commit()
    await db.refresh(story)

    logger.info("story_deploying", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)


@router.post("/{story_id}/test", response_model=StoryRead)
async def test_story(
    story_id: str,
    body: StoryTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    body = body or StoryTransition()
    story = await _get_story(story_id, db)

    _do_transition(story, StoryStatus.TESTING)
    await db.commit()
    await db.refresh(story)

    logger.info("story_testing", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)


@router.post("/{story_id}/reopen", response_model=StoryRead)
async def reopen_story(
    story_id: str,
    body: StoryReopen | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    body = body or StoryReopen()
    story = await _get_story(story_id, db)

    _do_transition(story, StoryStatus.REOPENED)
    if body.user_report is not None:
        story.user_report = body.user_report

    await db.commit()
    await db.refresh(story)

    logger.info("story_reopened", story_id=story.id, actor=body.actor)
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


@router.post("/{story_id}/send-to-architect", response_model=StoryRead)
async def send_to_architect(
    story_id: str,
    body: AdminAction | None = None,
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
) -> StoryRead:
    """Send a story to the architect for decomposition.

    Validates status is CREATED or REOPENED, transitions to IN_PROGRESS,
    and publishes ArchitectMessage to architect:queue.
    """
    body = body or AdminAction()
    story = await _get_story(story_id, db)

    allowed = {StoryStatus.CREATED.value, StoryStatus.REOPENED.value}
    if story.status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Cannot send to architect from status '{story.status}'."
                " Must be created or reopened."
            ),
        )

    is_reopen = story.status == StoryStatus.REOPENED.value

    # Transition: CREATED/REOPENED → IN_PROGRESS
    _do_transition(story, StoryStatus.IN_PROGRESS)
    await db.commit()
    await db.refresh(story)

    # Publish to architect queue
    msg = ArchitectMessage(
        story_id=story.id,
        project_id=str(story.project_id),
        user_id="",
        is_reopen=is_reopen,
        user_report=story.user_report if is_reopen else None,
    )
    await redis.publish_message(ARCHITECT_QUEUE, msg)

    logger.info("story_sent_to_architect", story_id=story.id, actor=body.actor, is_reopen=is_reopen)
    return StoryRead.model_validate(story, from_attributes=True)
