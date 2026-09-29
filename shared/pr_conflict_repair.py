"""Durable terminal failure for the one admitted conflict Task, within its cycle."""

from shared.contracts.dto.owner_notification import OwnerNotificationState
from shared.contracts.dto.pr_conflict_repair import (
    PR_CONFLICT_REPAIR_KEY,
    PRConflictRepairEvidence,
    repair_task_id,
)
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.story_failure import (
    StoryFailure,
    StoryFailureCode,
    story_failure_admin_text,
    story_failure_owner_text,
)


async def stop_failed_pr_repair(
    api, story_id: str, task_id: str, run_id: str, detail: str, source: str
) -> bool:
    """Owe both audiences before settling a terminal repair Task.

    Superseded tasks and unrelated human review retain their current ending.
    A lost response is reconciled on the next visit from the exact Task cause
    and both notice obligations; transport/admission failures propagate.
    """
    story = await api.get_story(story_id)
    cycle = story.reopened_at or story.created_at
    if task_id != repair_task_id(story_id, cycle):
        return False
    response = await api.request("GET", f"tasks/{task_id}/events")
    admissions = [
        event["details"][PR_CONFLICT_REPAIR_KEY]
        for event in response.json()
        if PR_CONFLICT_REPAIR_KEY in event["details"]
    ]
    if len(admissions) != 1:
        raise RuntimeError("Conflict repair admission evidence is missing or ambiguous")
    evidence = PRConflictRepairEvidence.model_validate(admissions[0])
    if (
        evidence.story_id != story.id
        or evidence.project_id != story.project_id
        or evidence.pr_number != story.pr_number
    ):
        return False
    if story.status == StoryStatus.WAITING_HUMAN_REVIEW:
        reason = story.quarantine_reason or {}
        if reason.get(
            "code"
        ) != StoryFailureCode.PR_CONFLICT_REPAIR_EXHAUSTED.value or task_id not in reason.get(
            "detail", ""
        ):
            return False
        recorded = StoryFailure.model_validate(reason)
        notice = await api.get_story_owner_notification(story_id)
        if (
            notice.text != story_failure_owner_text(recorded)
            or notice.admin_text
            != story_failure_admin_text(story.id, str(story.project_id), recorded)
            or notice.state is OwnerNotificationState.VOIDED
            or notice.admin_state in {None, OwnerNotificationState.VOIDED}
        ):
            raise RuntimeError("Conflict repair stop is missing its notice obligations")
        return True
    if story.status not in {StoryStatus.IN_PROGRESS, StoryStatus.PR_REVIEW}:
        return False
    failure = StoryFailure(
        code=StoryFailureCode.PR_CONFLICT_REPAIR_EXHAUSTED,
        source=source,
        detail=(
            f"PR #{evidence.pr_number}: repair Task {task_id}, Run {run_id}, "
            f"iteration ceiling {evidence.max_iterations}. {detail} "
            "The one automatic repair Task ended without a usable result."
        ),
    )
    await api.stop_story(story.id, "human-review", failure, actor=source)
    return True
