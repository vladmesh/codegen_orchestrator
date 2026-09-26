"""The outcome of a story's planning, and the operator's way back from a failed one.

`POST /stories/{id}/planning-outcome` is how the architect reports one planning
attempt. On the locked story row it decides what the attempt means: a success
records the channels that planned it; a failure another try may clear is
counted and scheduled for a retry with backoff; a failure past the retry bound,
or one no retry clears, parks the story in `waiting_human_review` with its
`planning_failed` `StoryFailure` and the owed owner and admin notices — the same
stop `POST /stories/{id}/human-review` makes, in the same transaction.

`POST /stories/{id}/retry-planning` is the operator's re-run of a parked
planning failure. In one transaction it clears the stop, returns the story to
in_progress and writes the planning record as `retrying`, due now, with the
count reset: from then on planning is owed durably, and the scheduler's
supervisor publishes it like any other due retry. Publishing right after the
commit only saves a tick; when it cannot, the request still succeeds and the
supervisor publishes. No SQL, no status patch.
"""

from contextlib import suppress
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
    PLANNING_RETRY_GUARD_TTL_CONFIG_KEY,
    StoryPlanning,
    StoryPlanningOutcome,
    StoryPlanningReport,
    StoryPlanningState,
    failed_record,
    operator_retry_record,
    planned_record,
    planning_retry_queued_key,
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


async def _config_int(db: AsyncSession, key: str) -> int:
    row = await db.get(SystemConfig, key)
    if row is None:
        raise RuntimeError(f"Missing system config: {key}")
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
        planning = planned_record(
            report,
            planning_attempt_id=report.planning_attempt_id,
            reopen=report.reopen,
            now=now,
        )
    else:
        if story.status not in _PLANNING_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Story is '{story.status}', no longer being planned",
            )
        planning = failed_record(
            _recorded_planning(story),
            report,
            max_retries=await _config_int(db, PLANNING_MAX_RETRIES_CONFIG_KEY),
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
    `planning_failed` `StoryFailure`; anything else is refused with 422. One
    transaction clears the stop, lands on in_progress and writes `retrying`
    due now with the count reset, so the re-run is owed on the row before
    anything is published. The response is that story either way: published
    now, or left to the supervisor's next tick when the immediate publish
    fails. The architect's claim voids the failed attempt's unadmitted tasks,
    so nothing of the failed plan survives into the new one.
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
    guard_ttl = await _config_int(db, PLANNING_RETRY_GUARD_TTL_CONFIG_KEY)
    planning = operator_retry_record(
        _recorded_planning(story),
        max_retries=await _config_int(db, PLANNING_MAX_RETRIES_CONFIG_KEY),
        now=datetime.now(UTC),
    )
    story.quarantine_reason = None
    story.planning = planning.model_dump(mode="json")
    _do_transition(story, StoryStatus.IN_PROGRESS)
    await db.commit()
    await db.refresh(story)

    published = await _publish_owed_planning(story, planning, guard_ttl, db, redis)
    logger.info(
        "story_planning_retried",
        story_id=story.id,
        actor=actor,
        requested_by=body.actor,
        is_reopen=planning.reopen,
        published=published,
    )
    return StoryRead.model_validate(story, from_attributes=True)


async def _publish_owed_planning(
    story: Story,
    planning: StoryPlanning,
    guard_ttl: int,
    db: AsyncSession,
    redis: RedisStreamClient,
) -> bool:
    """Publish the architect job the committed record owes, now. Best effort.

    The record on the row is what is owed, and it is already committed; the
    supervisor publishes it whenever this does not. So every failure is logged
    and left to it. The supervisor's throttle is set only after the publish
    returned, so a failure here leaves nothing that could hold the supervisor
    back; a duplicate the supervisor may still send is settled by the architect.
    """
    try:
        telegram_chat_id = await resolve_project_chat_id(
            db, story.project_id, event="story_planning_retried", story_id=story.id
        )
        await redis.publish_message(
            ARCHITECT_QUEUE,
            ArchitectMessage(
                story_id=story.id,
                project_id=str(story.project_id),
                telegram_chat_id=telegram_chat_id,
                is_reopen=planning.reopen,
                user_report=story.user_report if planning.reopen else None,
            ),
        )
    except Exception as exc:
        logger.warning(
            "story_planning_retry_left_to_supervisor",
            story_id=story.id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False
    with suppress(Exception):
        # Only saves the supervisor a duplicate; losing it costs nothing more.
        await redis.redis.set(planning_retry_queued_key(story.id, planning), 1, ex=guard_ttl)
    return True
