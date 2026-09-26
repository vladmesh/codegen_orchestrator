"""The outcome of a story's planning, and the operator's way back from a failed one.

`POST /stories/{id}/planning-outcome` is how the architect reports one planning
attempt. On the locked story row it decides what the attempt means: a success
records the channels that planned it; a failure another try may clear is
counted and scheduled for a retry with backoff; a failure past the retry bound,
or one no retry clears, parks the story in `waiting_human_review` with its
`planning_failed` `StoryFailure` and the owed owner and admin notices — the same
stop `POST /stories/{id}/human-review` makes, in the same transaction.

`POST /stories/{id}/retry-planning` is the operator's re-run of a parked
planning failure: it clears the failure and the retry count, returns the story
to in_progress and queues the architect once. No SQL, no status patch.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.story_failure import (
    STORY_FAILURE_REASON,
    StoryFailure,
    StoryFailureCode,
)
from shared.contracts.dto.story_planning import (
    PLANNING_MAX_RETRIES_CONFIG_KEY,
    StoryPlanning,
    StoryPlanningOutcome,
    StoryPlanningReport,
    StoryPlanningState,
    failed_record,
    planned_record,
)
from shared.contracts.queues.architect import ArchitectMessage
from shared.models import SystemConfig
from shared.models.story import Story
from shared.queues import ARCHITECT_QUEUE
from shared.redis.client import RedisStreamClient

from ..database import get_async_session
from ..dependencies import get_internal_or_admin_actor, get_redis_client, require_internal_or_admin
from ..schemas.actions import AdminAction
from ..schemas.story import StoryRead
from ._recipients import resolve_project_chat_id
from ._story_actions import COMPOSITE_CHAINS, PARK_UNSTARTED_PLANNING_FAILURE, _apply_chain
from ._story_helpers import _do_transition, _get_story_for_update, _record_story_failure

logger = structlog.get_logger()

planning_router = APIRouter()

#: The statuses a story is planned in: the architect starts a `created` story
#: before planning it and a `reopened` one only after. A failure reported for a
#: story anywhere else is stale — the story already moved on.
_PLANNING_STATUSES = frozenset(
    {StoryStatus.CREATED.value, StoryStatus.IN_PROGRESS.value, StoryStatus.REOPENED.value}
)


def _recorded_planning(story: Story) -> StoryPlanning | None:
    return None if story.planning is None else StoryPlanning.model_validate(story.planning)


def planning_failure_of(story: Story) -> StoryFailure | None:
    """The `planning_failed` stop the story is parked with, or None."""
    reason = story.quarantine_reason
    if not isinstance(reason, dict) or reason.get("reason") != STORY_FAILURE_REASON:
        return None
    try:
        failure = StoryFailure.model_validate(reason)
    except ValidationError:
        return None
    return failure if failure.code is StoryFailureCode.PLANNING_FAILED else None


async def _max_planning_retries(db: AsyncSession) -> int:
    row = await db.get(SystemConfig, PLANNING_MAX_RETRIES_CONFIG_KEY)
    if row is None:
        raise RuntimeError(f"Missing system config: {PLANNING_MAX_RETRIES_CONFIG_KEY}")
    return int(row.value)


def _park(story: Story, failure: StoryFailure) -> None:
    """The `human-review` stop, with the reason and both owed notices, on this row."""
    if story.status == StoryStatus.IN_PROGRESS.value:
        _do_transition(story, StoryStatus.WAITING_HUMAN_REVIEW)
    else:
        _apply_chain(story, COMPOSITE_CHAINS[PARK_UNSTARTED_PLANNING_FAILURE])
    _record_story_failure(story, failure, StoryStatus.WAITING_HUMAN_REVIEW)


@planning_router.post("/{story_id}/planning-outcome", response_model=StoryRead)
async def record_planning_outcome(
    story_id: str,
    report: StoryPlanningReport,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> StoryRead:
    """Record one planning attempt's outcome and decide what the story does next.

    A failure is refused with 409 once the story left the planning statuses: a
    stale report must not park a story that moved on. The retry count is the
    one on the row, so every decision reads the count the previous one wrote.
    """
    story = await _get_story_for_update(story_id, db)
    now = datetime.now(UTC)
    if report.outcome is StoryPlanningOutcome.SUCCEEDED:
        planning = planned_record(report, now)
    else:
        if story.status not in _PLANNING_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Story is '{story.status}', no longer being planned",
            )
        planning = failed_record(
            _recorded_planning(story),
            report,
            max_retries=await _max_planning_retries(db),
            now=now,
        )
        if planning.state is StoryPlanningState.PARKED:
            _park(story, report.failure)
    story.planning = planning.model_dump(mode="json")
    await db.commit()
    await db.refresh(story)
    logger.info(
        "story_planning_outcome_recorded",
        story_id=story.id,
        actor=report.actor,
        outcome=report.outcome.value,
        planning_state=planning.state.value,
        failed_attempts=planning.failed_attempts,
        retriable=report.retriable,
        story_status=story.status,
        llm_channels=planning.channels,
        llm_channel_failures=planning.channel_failures,
    )
    return StoryRead.model_validate(story, from_attributes=True)


@planning_router.post("/{story_id}/retry-planning", response_model=StoryRead)
async def retry_story_planning(
    story_id: str,
    body: AdminAction | None = None,
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
    actor: str = Depends(get_internal_or_admin_actor),
) -> StoryRead:
    """Re-run planning for a story parked by a planning failure.

    Valid only for a story in `waiting_human_review` whose recorded stop is a
    `planning_failed` `StoryFailure`; anything else is refused with 422. In one
    transaction the failure and the retry count are cleared and the story
    returns to in_progress; then one `ArchitectMessage` is published. The
    architect's claim voids the failed attempt's unadmitted tasks, so nothing
    of the failed plan survives into the new one.
    """
    body = body or AdminAction()
    story = await _get_story_for_update(story_id, db)
    if story.status != StoryStatus.WAITING_HUMAN_REVIEW.value or planning_failure_of(story) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "retry-planning requires a story parked in waiting_human_review with a "
                f"planning_failed reason; story {story.id} is '{story.status}'"
            ),
        )
    previous = _recorded_planning(story)
    reopen = previous is not None and previous.reopen
    story.quarantine_reason = None
    story.planning = None
    _do_transition(story, StoryStatus.IN_PROGRESS)
    await db.commit()
    await db.refresh(story)

    msg = ArchitectMessage(
        story_id=story.id,
        project_id=str(story.project_id),
        telegram_chat_id=await resolve_project_chat_id(
            db, story.project_id, event="story_planning_retried", story_id=story.id
        ),
        is_reopen=reopen,
        user_report=story.user_report if reopen else None,
    )
    await redis.publish_message(ARCHITECT_QUEUE, msg)
    logger.info(
        "story_planning_retried",
        story_id=story.id,
        actor=actor,
        requested_by=body.actor,
        is_reopen=reopen,
    )
    return StoryRead.model_validate(story, from_attributes=True)
