"""Story router helpers — the shared row readers and transition validator.

Both the single-hop action endpoints in ``stories.py`` and the composite
actions in ``_story_actions.py`` read and validate a Story through these, so a
Story has exactly one locking reader and one transition validator no matter
which endpoint moves it.
"""

from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.owner_notification import OwnerNotification, OwnerNotificationState
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.story import (
    VALID_TRANSITIONS,
    WAITING_ON_BY_STATUS,
    StoryStatus,
)
from shared.contracts.dto.story_failure import (
    CLOSED_TASK_STATUSES,
    StoryFailure,
    story_failure_admin_text,
    story_failure_owner_text,
)
from shared.contracts.vocab import OwnerNotificationEvent
from shared.models import Task
from shared.models.run import Run
from shared.models.story import Story

_TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}
)


async def _load_story(story_id: str, db: AsyncSession, *, for_update: bool) -> Story:
    query = select(Story).where(Story.id == story_id)
    if for_update:
        query = query.with_for_update()
    result = await db.execute(query)
    story = result.scalar_one_or_none()
    if not story:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Story {story_id} not found",
        )
    return story


async def _get_story(story_id: str, db: AsyncSession) -> Story:
    """Read a story without taking a row lock — read-only paths only."""
    return await _load_story(story_id, db, for_update=False)


async def _get_story_for_update(story_id: str, db: AsyncSession) -> Story:
    """Read a story with SELECT ... FOR UPDATE — every path that mutates the row.

    Two callers transitioning the same story then serialize on the row, so the
    second one re-reads the status the first committed and its transition is
    validated against that, not against a stale snapshot.
    """
    return await _load_story(story_id, db, for_update=True)


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


def _land_on(story: Story, to_status: StoryStatus) -> None:
    """Write a story's status and the ``waiting_on`` that status implies.

    The only assignment of ``Story.waiting_on`` in the codebase.  Both callers
    — ``_do_transition`` for a single hop and ``_apply_chain`` for a composite —
    reach the field through here, so the two fields are written together on one
    locked row inside the caller's transaction and can never disagree.  There is
    no poller-visible path to the column: ``StoryUpdate`` refuses it.
    """
    story.status = to_status.value
    story.waiting_on = WAITING_ON_BY_STATUS[to_status].value


def _do_transition(story: Story, to_status: StoryStatus) -> None:
    """Apply one validated hop to a locked story row."""
    _validate_transition(story.status, to_status.value)
    _land_on(story, to_status)


async def work_cycle_task_count(story: Story, db: AsyncSession) -> int:
    """Tasks of the story's current plan, by the rule of ``in_work_cycle``."""
    query = select(func.count()).select_from(Task).where(Task.story_id == story.id)
    if story.reopened_at is not None:
        query = query.where(
            or_(
                Task.created_at >= story.reopened_at,
                Task.status.not_in(CLOSED_TASK_STATUSES),
            )
        )
    return int(await db.scalar(query) or 0)


#: The owner event each landing a `StoryFailure` may accompany is told as.
_FAILURE_EVENT_BY_STATUS: dict[StoryStatus, OwnerNotificationEvent] = {
    StoryStatus.FAILED: OwnerNotificationEvent.STORY_FAILED,
    StoryStatus.WAITING_HUMAN_REVIEW: OwnerNotificationEvent.STORY_BLOCKED,
}


def _record_story_failure(story: Story, failure: StoryFailure, to_status: StoryStatus) -> None:
    """Write why a platform failure stopped the story, and owe its owner the notice.

    Called by the transition that lands on ``to_status``, before the commit, so
    the reason, the owed record and the status are one write: a reader never
    sees a failed story without its cause, and the owner-notification sweep
    delivers the notice even when the caller dies right after the commit.
    """
    story.quarantine_reason = failure.model_dump(mode="json")
    project_id = str(story.project_id)
    story.owner_notification = OwnerNotification(
        event=_FAILURE_EVENT_BY_STATUS[to_status],
        text=story_failure_owner_text(failure),
        story_id=story.id,
        project_id=project_id,
        terminal_status=to_status,
        state=OwnerNotificationState.OWED,
        owed_at=datetime.now(UTC),
        admin_text=story_failure_admin_text(story.id, project_id, failure),
        admin_state=OwnerNotificationState.OWED,
    ).model_dump(mode="json")


async def _record_qa_routing(
    story: Story, qa_run_id: str | None, to_status: StoryStatus, db: AsyncSession
) -> None:
    """Stamp the QA run whose verdict moves a TESTING story, in the move's transaction.

    Temporary-access cleanup escalation waits for this stamp before it records
    an incident against a QA run with a verdict, so the stamp has to mean
    exactly "this story consumed this run's verdict". ``Run.qa_routed_at`` is
    written only here, under the story lock and then the run lock, and only by
    a transition out of TESTING that names the run; no run create or update
    schema carries the column, and any other status change leaves it unset.
    """
    if qa_run_id is None:
        return
    run = await db.get(Run, qa_run_id, with_for_update=True)
    if (
        story.status != StoryStatus.TESTING.value
        or run is None
        or run.type != RunType.QA.value
        or run.story_id != story.id
        or run.status not in _TERMINAL_RUN_STATUSES
        or not isinstance(run.result, dict)
        or run.result.get("qa_outcome") is None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"QA run {qa_run_id} is not a verdict this TESTING story can route",
        )
    run.qa_routed_at = datetime.now(UTC)
