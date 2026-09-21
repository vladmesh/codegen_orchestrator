"""The liveness supervisor's side of a pre-agent infrastructure park.

Admission parks its own refusals in the transaction that decides them, so the
dispatcher never parks anything. A post-handoff refusal carried by a refused Run
is parked by one API transaction,
`POST /api/stories/{id}/park-infrastructure-refusal`, which proves the evidence
against that Run and commits the task and story transitions, both copies of the
evidence, and the owed owner and administrator notices together or not at all.
This module builds that call and returns its typed disposition; delivery belongs
to `supervise_owed_owner_notifications`, so no delivery outcome can decide
whether the park happened.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from shared.contracts.dto.engineering_execution import (
    EngineeringInfrastructurePark,
    EngineeringInfrastructureParkCommand,
    EngineeringInfrastructureParkDisposition,
)
from shared.contracts.dto.task import TaskDTO

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient


async def park_story_infrastructure_refusal(
    api_client: SchedulerAPIClient,
    task: TaskDTO,
    park: EngineeringInfrastructurePark,
    *,
    actor: str,
    log: structlog.stdlib.BoundLogger,
) -> EngineeringInfrastructureParkDisposition:
    """Ask the API to park a Run-backed refusal atomically and return its answer."""
    park = EngineeringInfrastructurePark.model_validate(park)
    if park.task_id != task.id:
        raise ValueError("infrastructure park task does not match the refused task")
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
    return read.disposition
