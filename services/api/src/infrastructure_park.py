"""The one writer of a pre-agent infrastructure park.

Two callers reach it, each inside its own transaction and on rows it already
holds in the repository lock ladder (Task, then Story): admission, for the paid
refusal it has just decided and audited, and the park endpoint, once a locked
refused Run or the unique committed admission audit proves the exact evidence.
Nothing here commits, so a park is observed complete or not at all.

A story-backed park owes both notification audiences before commit: the owner
through the existing terminal-notification record and administrators through
that record's independent audience, which the owner-notification supervisor
delivers and settles separately.
"""

from datetime import UTC, datetime
from typing import NoReturn

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.engineering_execution import (
    ENGINEERING_INFRASTRUCTURE_KEY,
    EngineeringInfrastructurePark,
    EngineeringInfrastructureParkDisposition,
)
from shared.contracts.dto.owner_notification import OwnerNotification, OwnerNotificationState
from shared.contracts.dto.story import VALID_TRANSITIONS as STORY_TRANSITIONS, StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.vocab import OwnerNotificationEvent
from shared.models import Task
from shared.models.story import Story

logger = structlog.get_logger()

INFRASTRUCTURE_PARK_ACTION = "park_infrastructure_refusal"

#: Task statuses a real pre-agent refusal is observed in: an admission refusal
#: (`todo`), an operator respawn or no-Run refusal that already left todo
#: (`in_dev`), and a post-handoff refusal the result handler already failed
#: (`failed`), each with its legal hops to human review.
PARKABLE_TASK_HOPS: dict[str, tuple[TaskStatus, ...]] = {
    TaskStatus.TODO.value: (TaskStatus.IN_DEV, TaskStatus.WAITING_HUMAN_REVIEW),
    TaskStatus.IN_DEV.value: (TaskStatus.WAITING_HUMAN_REVIEW,),
    TaskStatus.FAILED.value: (TaskStatus.WAITING_HUMAN_REVIEW,),
}


def infrastructure_conflict(code: str, message: str) -> NoReturn:
    """A typed fail-closed refusal; the caller's transaction commits nothing."""
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": code, "message": message},
    )


def _admin_notice(task: Task, story: Story, park: EngineeringInfrastructurePark) -> str:
    return (
        f"Engineering infrastructure refusal parked story {story.id} "
        f"(project {story.project_id}, task {task.id}, attempt {park.attempt_id}): "
        f"{park.refusal.value}. {park.detail}"
    )


async def apply_infrastructure_park(
    task: Task,
    story: Story | None,
    park: EngineeringInfrastructurePark,
    *,
    actor: str,
    db: AsyncSession,
) -> EngineeringInfrastructureParkDisposition:
    """Park one exact refusal on already-locked rows, without committing.

    A repeat of the same park is `already_parked` and writes nothing. A terminal
    or otherwise transition-ineligible story is `ineligible_story` and changes
    neither row. Different or half-applied evidence, a row already in human
    review for another reason, and a non-parkable task status raise a typed 409.
    """
    from .routers._story_helpers import _do_transition
    from .routers._task_helpers import create_status_event, validate_transition

    if park.task_id != task.id or (story is not None and task.story_id != story.id):
        infrastructure_conflict("wrong_task", "The park does not name this task and story.")
    evidence = park.model_dump(mode="json")
    task_evidence = (task.failure_metadata or {}).get(ENGINEERING_INFRASTRUCTURE_KEY)
    story_evidence = (
        None
        if story is None
        else (story.quarantine_reason or {}).get(ENGINEERING_INFRASTRUCTURE_KEY)
    )
    if any(found not in (None, evidence) for found in (task_evidence, story_evidence)):
        infrastructure_conflict(
            "stale_infrastructure_reason", "A different infrastructure park is recorded."
        )
    task_parked = task.status == TaskStatus.WAITING_HUMAN_REVIEW.value and task_evidence == evidence
    story_parked = story is None or (
        story.status == StoryStatus.WAITING_HUMAN_REVIEW.value and story_evidence == evidence
    )
    if task_parked and story_parked:
        return EngineeringInfrastructureParkDisposition.ALREADY_PARKED
    if task_evidence is not None or story_evidence is not None:
        infrastructure_conflict(
            "park_state_changed", "The recorded park no longer has its parked state."
        )
    if task.status == TaskStatus.WAITING_HUMAN_REVIEW.value or (
        story is not None and story.status == StoryStatus.WAITING_HUMAN_REVIEW.value
    ):
        infrastructure_conflict(
            "already_in_human_review", "Story or task is already in human review."
        )
    if (
        story is not None
        and StoryStatus.WAITING_HUMAN_REVIEW not in STORY_TRANSITIONS[StoryStatus(story.status)]
    ):
        # A terminal or otherwise ineligible story wins: nothing is reopened,
        # no evidence is written, and the task is left exactly as it was.
        logger.warning(
            "engineering_infrastructure_story_ineligible",
            story_id=story.id,
            task_id=task.id,
            story_status=story.status,
        )
        return EngineeringInfrastructureParkDisposition.INELIGIBLE_STORY
    hops = PARKABLE_TASK_HOPS.get(task.status)
    if hops is None:
        infrastructure_conflict(
            "wrong_status", "Infrastructure park requires a todo, in_dev, or failed task."
        )

    audit = {"action": INFRASTRUCTURE_PARK_ACTION, **evidence}
    for hop in hops:
        validate_transition(task.status, hop)
        from_status = task.status
        task.status = hop.value
        await create_status_event(task, from_status, hop, actor, audit, db)
    task.failure_metadata = {**(task.failure_metadata or {}), **park.as_metadata()}
    if story is not None:
        story.quarantine_reason = {**(story.quarantine_reason or {}), **park.as_metadata()}
        _do_transition(story, StoryStatus.WAITING_HUMAN_REVIEW)
        story.owner_notification = OwnerNotification(
            event=OwnerNotificationEvent.STORY_BLOCKED,
            text=park.detail,
            story_id=story.id,
            project_id=str(story.project_id),
            terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
            state=OwnerNotificationState.OWED,
            owed_at=datetime.now(UTC),
            admin_text=_admin_notice(task, story, park),
            admin_state=OwnerNotificationState.OWED,
        ).model_dump(mode="json")
    logger.info(
        "engineering_infrastructure_refusal_parked",
        story_id=None if story is None else story.id,
        task_id=task.id,
        attempt_id=park.attempt_id,
        refusal=park.refusal.value,
        actor=actor,
    )
    return EngineeringInfrastructureParkDisposition.PARKED
