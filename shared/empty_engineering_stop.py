"""Recognize only this empty-result handoff's committed story stop."""

from typing import Protocol

from pydantic import ValidationError

from shared.contracts.dto.owner_notification import OwnerNotification, OwnerNotificationState
from shared.contracts.dto.story import StoryDTO, StoryStatus, StoryWaitingOn
from shared.contracts.dto.story_failure import (
    StoryFailure,
    StoryFailureCode,
    story_failure_admin_text,
    story_failure_owner_text,
)
from shared.contracts.vocab import OwnerNotificationEvent


class EmptyStoryStopClient(Protocol):
    async def get_story(self, story_id: str) -> StoryDTO: ...
    async def get_story_owner_notification(self, story_id: str) -> OwnerNotification: ...
    async def stop_story(
        self, story_id: str, action: str, failure: StoryFailure, *, actor: str
    ) -> StoryDTO: ...


def matching_empty_cause(story: StoryDTO, expected: StoryFailure) -> StoryFailure | None:
    """Match the attempt-specific cause, never human review alone."""
    if (
        story.status is not StoryStatus.WAITING_HUMAN_REVIEW
        or story.waiting_on is not StoryWaitingOn.HUMAN_REVIEW
        or story.quarantine_reason is None
    ):
        return None
    try:
        recorded = StoryFailure.model_validate(story.quarantine_reason)
    except ValidationError:
        return None
    if (
        recorded.code is StoryFailureCode.NO_NEW_COMMIT
        and expected.code is StoryFailureCode.NO_NEW_COMMIT
        and recorded.source == expected.source
        and recorded.detail == expected.detail
    ):
        return recorded
    return None


async def _committed(api: EmptyStoryStopClient, story_id: str, failure: StoryFailure) -> bool:
    story = await api.get_story(story_id)
    recorded = matching_empty_cause(story, failure)
    if recorded is None:
        return False
    notice = await api.get_story_owner_notification(story_id)
    return (
        notice.story_id == story.id
        and notice.project_id == str(story.project_id)
        and notice.terminal_status is StoryStatus.WAITING_HUMAN_REVIEW
        and notice.event is OwnerNotificationEvent.STORY_BLOCKED
        and notice.task_id is None
        and notice.owed_at >= recorded.observed_at
        and notice.state is not OwnerNotificationState.VOIDED
        and notice.admin_state is not None
        and notice.admin_state is not OwnerNotificationState.VOIDED
        and notice.text == story_failure_owner_text(recorded)
        and notice.admin_text == story_failure_admin_text(story.id, str(story.project_id), recorded)
    )


async def ensure_empty_story_stop(
    api: EmptyStoryStopClient, story_id: str, failure: StoryFailure, *, actor: str
) -> None:
    """Resume remaining effects without repeating an already committed transition.

    A lost stop response is accepted only after reading both its exact cause and
    its notification episode. Refused or unrelated stops still raise.
    """
    if await _committed(api, story_id, failure):
        return
    story = await api.get_story(story_id)
    if story.status is StoryStatus.WAITING_HUMAN_REVIEW:
        raise RuntimeError("The Story is already stopped by another cause or notification episode")
    try:
        await api.stop_story(story_id, "human-review", failure, actor=actor)
    except Exception:
        if await _committed(api, story_id, failure):
            return
        raise
