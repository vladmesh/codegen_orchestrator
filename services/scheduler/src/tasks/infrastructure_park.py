"""One ordered reconciliation boundary for pre-agent infrastructure parks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from shared.contracts.dto.engineering_execution import (
    ENGINEERING_INFRASTRUCTURE_KEY,
    EngineeringInfrastructurePark,
)
from shared.contracts.dto.owner_notification import OwnerNotificationState
from shared.contracts.dto.story import VALID_TRANSITIONS, StoryStatus
from shared.contracts.dto.task import TaskDTO, TaskStatus
from shared.contracts.vocab import OwnerNotificationEvent
from shared.redis import RedisStreamClient

from .owner_notifications import (
    OwnerNotificationOutcome,
    deliver_owed_notification,
    owe_owner_notification,
    owe_story_owner_notification,
    read_story_owner_notification,
)

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient


class InfrastructureParkDisposition(StrEnum):
    """Contained result of reconciling one exact infrastructure refusal."""

    PARKED = "parked"
    ALREADY_PARKED = "already_parked"
    INELIGIBLE_STORY = "ineligible_story"
    STORY_STATE_RACE = "story_state_race"
    NOTIFICATION_PENDING = "notification_pending"


_SETTLED_NOTIFICATION_OUTCOMES = frozenset(
    {
        OwnerNotificationOutcome.DELIVERED,
        OwnerNotificationOutcome.EXHAUSTED,
        OwnerNotificationOutcome.SKIPPED,
        OwnerNotificationOutcome.UNADDRESSABLE,
    }
)


async def _reconcile_standalone_task(
    api_client: SchedulerAPIClient,
    task: TaskDTO,
    metadata: dict,
    evidence: dict,
    *,
    actor: str,
    evidence_matches: bool,
) -> InfrastructureParkDisposition:
    if not evidence_matches:
        await api_client.update_task(task.id, {"failure_metadata": metadata})
    if task.status == TaskStatus.TODO:
        await api_client.transition_task(task.id, TaskStatus.IN_DEV, actor)
    if task.status == TaskStatus.WAITING_HUMAN_REVIEW:
        return InfrastructureParkDisposition.ALREADY_PARKED
    await api_client.transition_task(
        task.id,
        TaskStatus.WAITING_HUMAN_REVIEW,
        actor,
        details=evidence,
    )
    return InfrastructureParkDisposition.PARKED


def _story_precedence(story, evidence: dict, log) -> InfrastructureParkDisposition | None:
    story_evidence = (story.quarantine_reason or {}).get(ENGINEERING_INFRASTRUCTURE_KEY)
    if story.status == StoryStatus.WAITING_HUMAN_REVIEW:
        if story_evidence == evidence:
            return None
        log.warning(
            "engineering_infrastructure_story_already_parked",
            story_status=story.status.value,
        )
        return InfrastructureParkDisposition.INELIGIBLE_STORY
    if StoryStatus.WAITING_HUMAN_REVIEW in VALID_TRANSITIONS[story.status]:
        return None
    log.warning(
        "engineering_infrastructure_story_ineligible",
        story_status=story.status.value,
    )
    return InfrastructureParkDisposition.INELIGIBLE_STORY


async def _settle_notifications(  # noqa: PLR0913
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    task: TaskDTO,
    story,
    park: EngineeringInfrastructurePark,
    *,
    notification_run: Any | None,
    notification_event: OwnerNotificationEvent,
    log,
) -> OwnerNotificationOutcome:
    if notification_run is None:
        owed = read_story_owner_notification(story)
        if owed is None or owed.state is OwnerNotificationState.VOIDED:
            owed = await owe_story_owner_notification(
                api_client,
                task.story_id,
                event=notification_event,
                text=park.detail,
                project_id=str(task.project_id),
                terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
                log=log,
            )
        return await deliver_owed_notification(
            api_client,
            redis_client,
            task.story_id,
            owed,
            log,
            story_record=True,
        )

    owed = await owe_owner_notification(
        api_client,
        notification_run,
        event=notification_event,
        text=park.detail,
        story_id=task.story_id,
        project_id=str(task.project_id),
        terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
        task_id=task.id,
        log=log,
    )
    return await deliver_owed_notification(api_client, redis_client, notification_run.id, owed, log)


async def reconcile_pre_agent_infrastructure_park(  # noqa: PLR0913
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    task: TaskDTO,
    park: EngineeringInfrastructurePark,
    *,
    notification_run: Any | None,
    notification_event: OwnerNotificationEvent,
    actor: str,
    notify_admin: Callable[[str, str, str], Awaitable[None]],
    log: structlog.stdlib.BoundLogger,
) -> InfrastructureParkDisposition:
    """Converge evidence, story, notifications, then task in that exact order."""
    park = EngineeringInfrastructurePark.model_validate(park)
    if park.task_id != task.id:
        raise ValueError("infrastructure park task does not match reconciled task")
    metadata = park.as_metadata()
    evidence = metadata[ENGINEERING_INFRASTRUCTURE_KEY]
    task_evidence_matches = (task.failure_metadata or {}).get(
        ENGINEERING_INFRASTRUCTURE_KEY
    ) == evidence

    if not task.story_id:
        return await _reconcile_standalone_task(
            api_client,
            task,
            metadata,
            evidence,
            actor=actor,
            evidence_matches=task_evidence_matches,
        )

    story = await api_client.get_story(task.story_id)
    story_evidence_matches = (story.quarantine_reason or {}).get(
        ENGINEERING_INFRASTRUCTURE_KEY
    ) == evidence
    task_parked = task.status == TaskStatus.WAITING_HUMAN_REVIEW and task_evidence_matches
    story_parked = story.status == StoryStatus.WAITING_HUMAN_REVIEW and story_evidence_matches
    if task_parked and story_parked:
        return InfrastructureParkDisposition.ALREADY_PARKED

    if precedence := _story_precedence(story, evidence, log):
        return precedence

    if not task_evidence_matches:
        await api_client.update_task(task.id, {"failure_metadata": metadata})
    if not story_evidence_matches:
        await api_client.update_story(task.story_id, {"quarantine_reason": metadata})

    if story.status != StoryStatus.WAITING_HUMAN_REVIEW:
        await api_client.transition_story(task.story_id, "human-review")
        story = await api_client.get_story(task.story_id)

    observed_evidence = (story.quarantine_reason or {}).get(ENGINEERING_INFRASTRUCTURE_KEY)
    if story.status != StoryStatus.WAITING_HUMAN_REVIEW or observed_evidence != evidence:
        log.warning(
            "engineering_infrastructure_story_park_raced",
            story_status=story.status.value,
        )
        return InfrastructureParkDisposition.STORY_STATE_RACE

    outcome = await _settle_notifications(
        api_client,
        redis_client,
        task,
        story,
        park,
        notification_run=notification_run,
        notification_event=notification_event,
        log=log,
    )
    if outcome not in _SETTLED_NOTIFICATION_OUTCOMES:
        return InfrastructureParkDisposition.NOTIFICATION_PENDING

    if outcome is not OwnerNotificationOutcome.SKIPPED:
        await notify_admin(task.id, str(task.project_id), park.detail)
    if task.status == TaskStatus.TODO:
        await api_client.transition_task(task.id, TaskStatus.IN_DEV, actor)
    if task.status != TaskStatus.WAITING_HUMAN_REVIEW:
        await api_client.transition_task(
            task.id,
            TaskStatus.WAITING_HUMAN_REVIEW,
            actor,
            details=evidence,
        )
    return InfrastructureParkDisposition.PARKED
