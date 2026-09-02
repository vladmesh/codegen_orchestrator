"""Composite Story actions — the one place a multi-hop Story move is declared.

A composite move used to be a sequence of `POST /stories/{id}/{action}` calls
issued by a poller or a consumer: a crash or a 422 between two of them left the
story parked in an intermediate status with nobody to finish it. Here the whole
move is one endpoint call — one locked row, one transaction, every hop checked
against ``VALID_TRANSITIONS`` before any hop is applied — so clients report the
event that happened and never sequence lifecycle state themselves.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.story import StoryStatus
from shared.models.story import Story

from ..database import get_async_session
from ..schemas.story import StoryRead, StoryTransition
from ._story_helpers import _get_story_for_update, _land_on, _validate_transition

logger = structlog.get_logger()

action_router = APIRouter()

#: The CI-failure retry: the PR poller has recorded the failed CI run and
#: created the fix task, so the story records the failed attempt, opens a new
#: work cycle and goes back to engineering.  Was three client calls
#: (`fail` → `reopen` → `start`) in `pr_poller._record_ci_failure`.
RETRY_AFTER_CI_FAILURE = "retry-after-ci-failure"

#: Every composite Story move the platform performs, as the ordered chain of
#: hops it applies.  Nothing outside this table walks a Story through more than
#: one status; a new composite is a new entry here plus its endpoint below.
COMPOSITE_CHAINS: dict[str, tuple[StoryStatus, ...]] = {
    RETRY_AFTER_CI_FAILURE: (
        StoryStatus.FAILED,
        StoryStatus.REOPENED,
        StoryStatus.IN_PROGRESS,
    ),
}


def _apply_chain(story: Story, chain: tuple[StoryStatus, ...]) -> None:
    """Validate every hop of the chain against VALID_TRANSITIONS, then apply it.

    The validation pass writes nothing, so an illegal hop anywhere in the chain
    raises 422 with the story exactly as it was.  Partial application is
    impossible: the first write happens only once the whole chain is known to
    be legal, and all of them commit together with the caller's transaction.

    Only the status the chain lands on survives, and ``waiting_on`` lands with
    it: every hop writes both through ``_land_on``, so the committed row carries
    the wait its final status implies.
    """
    cursor = story.status
    for hop in chain:
        _validate_transition(cursor, hop.value)
        cursor = hop.value

    for hop in chain:
        # The same landing write the single-hop path uses, so a composite gets
        # `waiting_on` from the one mapping rather than a copy of it.
        _land_on(story, hop)
        if hop is StoryStatus.REOPENED:
            # Reopening starts the current work cycle, and completion reads
            # this stamp to refuse pre-reopen QA evidence.  A composite that
            # passes through REOPENED writes it exactly as the single hop does.
            story.reopened_at = datetime.now(UTC)


@action_router.post(f"/{{story_id}}/{RETRY_AFTER_CI_FAILURE}", response_model=StoryRead)
async def retry_story_after_ci_failure(
    story_id: str,
    body: StoryTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    """Send a story whose CI run failed back to engineering in one move.

    failed → reopened → in_progress, applied on one locked row.  The caller has
    already created the fix task; it reports the CI failure and nothing else.
    """
    body = body or StoryTransition()
    story = await _get_story_for_update(story_id, db)

    _apply_chain(story, COMPOSITE_CHAINS[RETRY_AFTER_CI_FAILURE])

    await db.commit()
    await db.refresh(story)

    logger.info("story_retried_after_ci_failure", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)
