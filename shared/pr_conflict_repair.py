"""Submit a scoped outcome; only the API transaction grants settlement."""

from shared.contracts.dto.pr_conflict_repair import (
    PR_CONFLICT_REPAIR_KEY,
    PRConflictRepairAttemptCommand,
    PRConflictRepairAttemptDisposition,
    PRConflictRepairAttemptRead,
    PRConflictRepairEvidence,
)


async def settle_pr_repair_attempt(
    api,
    story_id: str,
    task_id: str,
    run_id: str,
    detail: str,
    disposition: PRConflictRepairAttemptDisposition,
) -> PRConflictRepairAttemptRead:
    """Use immutable admission identity even if client reads race a new cycle.

    Transport/admission errors propagate for normal retry. The API ledger
    deduplicates a lost successful response under the native locks.
    """
    response = await api.request("GET", f"tasks/{task_id}/events")
    admissions = [
        event["details"][PR_CONFLICT_REPAIR_KEY]
        for event in response.json()
        if PR_CONFLICT_REPAIR_KEY in event["details"]
    ]
    if len(admissions) != 1:
        raise RuntimeError("Conflict repair admission evidence is missing or ambiguous")
    evidence = PRConflictRepairEvidence.model_validate(admissions[0])
    run = await api.get_run(run_id)
    iteration = run.run_metadata["iteration"]
    if type(iteration) is not int:
        raise RuntimeError("Conflict repair Run has no valid iteration identity")
    command = PRConflictRepairAttemptCommand(
        project_id=evidence.project_id,
        pr_number=evidence.pr_number,
        cycle_started_at=evidence.cycle_started_at,
        task_id=task_id,
        attempt_id=run_id,
        expected_iteration=iteration,
        disposition=disposition,
        detail=detail,
    )
    response = await api.request(
        "POST",
        f"stories/{story_id}/repair-pr-conflicts/attempt-outcome",
        json=command.model_dump(mode="json"),
    )
    return PRConflictRepairAttemptRead.model_validate(response.json())
