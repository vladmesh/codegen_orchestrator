"""The scheduler side of a pre-agent infrastructure park.

A story-backed park is one API transaction,
`POST /api/stories/{id}/park-infrastructure-refusal`: the exact evidence, the
legal task and story transitions, and the owner's durable notice commit together
or not at all. This module builds that call and acts on its typed disposition;
it sequences no task or story state of its own, and notification delivery is
left to `supervise_owed_owner_notifications`, so a delivery outcome can never
decide whether the park happened.

A standalone task has no story transaction to join. Its park stays a separate,
explicit path that never names a story.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import structlog

from shared.contracts.dto.engineering_execution import (
    ENGINEERING_INFRASTRUCTURE_KEY,
    EngineeringInfrastructurePark,
    EngineeringInfrastructureParkCommand,
    EngineeringInfrastructureParkDisposition,
)
from shared.contracts.dto.task import TaskDTO, TaskStatus

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient


def _exact_park(
    task: TaskDTO, park: EngineeringInfrastructurePark
) -> EngineeringInfrastructurePark:
    park = EngineeringInfrastructurePark.model_validate(park)
    if park.task_id != task.id:
        raise ValueError("infrastructure park task does not match the refused task")
    return park


async def park_story_infrastructure_refusal(  # noqa: PLR0913
    api_client: SchedulerAPIClient,
    task: TaskDTO,
    park: EngineeringInfrastructurePark,
    *,
    actor: str,
    notify_admin: Callable[[str, str, str], Awaitable[None]],
    log: structlog.stdlib.BoundLogger,
) -> EngineeringInfrastructureParkDisposition:
    """Ask the API to park a story-backed refusal atomically and act on the answer.

    Only the call that committed the park alerts administrators, so a repeat
    after a lost response or a restart never alerts twice. The owner's notice
    was owed inside the park transaction and is not touched here.
    """
    park = _exact_park(task, park)
    if not task.story_id:
        raise ValueError("a standalone task has no story park transaction")
    read = await api_client.park_infrastructure_refusal(
        task.story_id, EngineeringInfrastructureParkCommand(park=park, actor=actor)
    )
    log.info(
        "engineering_infrastructure_park_committed",
        disposition=read.disposition.value,
        attempt_id=park.attempt_id,
        refusal=park.refusal.value,
        task_status=read.task_status,
        story_status=read.story_status,
    )
    if read.disposition is EngineeringInfrastructureParkDisposition.PARKED:
        await notify_admin(task.id, str(task.project_id), park.detail)
    return read.disposition


async def park_standalone_infrastructure_refusal(
    api_client: SchedulerAPIClient,
    task: TaskDTO,
    park: EngineeringInfrastructurePark,
    *,
    actor: str,
) -> EngineeringInfrastructureParkDisposition:
    """Park a refusal of a task that belongs to no story, on the task alone."""
    park = _exact_park(task, park)
    if task.story_id:
        raise ValueError("a story-backed task must use the atomic story park")
    metadata = park.as_metadata()
    evidence = metadata[ENGINEERING_INFRASTRUCTURE_KEY]
    if (task.failure_metadata or {}).get(ENGINEERING_INFRASTRUCTURE_KEY) != evidence:
        await api_client.update_task(task.id, {"failure_metadata": metadata})
    if task.status == TaskStatus.WAITING_HUMAN_REVIEW:
        return EngineeringInfrastructureParkDisposition.ALREADY_PARKED
    if task.status == TaskStatus.TODO:
        await api_client.transition_task(task.id, TaskStatus.IN_DEV, actor)
    await api_client.transition_task(
        task.id,
        TaskStatus.WAITING_HUMAN_REVIEW,
        actor,
        details=evidence,
    )
    return EngineeringInfrastructureParkDisposition.PARKED
