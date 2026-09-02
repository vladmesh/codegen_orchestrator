"""Story router helpers — the shared row readers and transition validator.

Both the single-hop action endpoints in ``stories.py`` and the composite
actions in ``_story_actions.py`` read and validate a Story through these, so a
Story has exactly one locking reader and one transition validator no matter
which endpoint moves it.
"""

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.story import (
    VALID_TRANSITIONS,
    WAITING_ON_BY_STATUS,
    StoryStatus,
)
from shared.models.story import Story


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
