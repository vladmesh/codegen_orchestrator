"""The one INSTALL a package-build FEATURE's successful engineering Run pins its commit into.

The terminal Run writer creates it under the engineering lock ladder; every route to the
source's `done` requires it, so no dependent work is released before the INSTALL exists.
"""

from datetime import UTC, datetime

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
import structlog

from shared.contracts.dto.commit_publication import AttemptDisposition
from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.dto.local_install import (
    LocalPackageBuild,
    LocalPackageInstall,
    local_install_task_id,
    parse_install_target,
    pinned_local_install,
)
from shared.contracts.dto.pr_conflict_repair import cycle_stamp
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import EngineeringRunResult
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.story_failure import (
    CLOSED_TASK_STATUSES,
    StoryFailure,
    StoryFailureCode,
)
from shared.contracts.dto.task import TaskStatus, TaskType
from shared.models import Repository, Run, Task

from .attempt_disposition import locked_disposition

logger = structlog.get_logger()

#: The status-event detail key a refused handoff records on its source Task.
HANDOFF_REFUSAL_KEY = "local_package_handoff_refused"

_ACTIVE_SOURCE_STATUSES = frozenset(
    {
        TaskStatus.TODO.value,
        TaskStatus.IN_DEV.value,
        TaskStatus.IN_CI.value,
        TaskStatus.TESTING.value,
    }
)
_LIVE_RUN_STATUSES = frozenset({RunStatus.QUEUED.value, RunStatus.RUNNING.value})
_REVIEW_HOPS = {
    TaskStatus.TODO.value: [TaskStatus.IN_DEV, TaskStatus.WAITING_HUMAN_REVIEW],
    TaskStatus.IN_DEV.value: [TaskStatus.WAITING_HUMAN_REVIEW],
    TaskStatus.IN_CI.value: [TaskStatus.IN_DEV, TaskStatus.WAITING_HUMAN_REVIEW],
    TaskStatus.TESTING.value: [TaskStatus.IN_DEV, TaskStatus.WAITING_HUMAN_REVIEW],
}


class _Refused(Exception):
    """A current build whose output cannot form its INSTALL; the argument is the cause."""


def _owned_attempt(task: Task, run: Run) -> bool:
    """Whether the Run is this Task's own engineering attempt at its current iteration."""
    iteration = (run.run_metadata or {}).get("iteration")
    return (
        run.type == RunType.ENGINEERING.value
        and run.task_id == task.id
        and run.project_id == task.project_id
        and run.story_id == task.story_id
        and type(iteration) is int
        and iteration == task.current_iteration
    )


def _pinned_target(task: Task, run: Run) -> LocalPackageInstall:
    """The INSTALL target the Run's typed successful outcome pins, else `_Refused`."""
    if run.status != RunStatus.COMPLETED.value or run.result is None:
        raise _Refused("engineering_result_missing")
    try:
        result = EngineeringRunResult.model_validate(run.result)
    except ValidationError as exc:
        raise _Refused("engineering_result_malformed") from exc
    if (
        result.engineering_status is not EngineeringStatus.DONE
        or result.failure_reason is not None
        or result.publication is not None
    ):
        raise _Refused("engineering_unsuccessful")
    if not result.commit_sha:
        raise _Refused("commit_missing")
    build = LocalPackageBuild.model_validate(task.package_build)
    try:
        return pinned_local_install(build, task.project_id, task.repository_id, result.commit_sha)
    except ValidationError as exc:
        raise _Refused("commit_invalid") from exc


def _is_pinned_child(child: Task, task: Task, target: LocalPackageInstall) -> bool:
    """Compares the child's immutable creation identity, never its later execution."""
    if child.type != TaskType.INSTALL.value or child.install is None:
        return False
    try:
        stored = parse_install_target(child.install)
    except ValidationError:
        return False
    return (
        child.project_id,
        child.story_id,
        child.repository_id,
        child.planning_attempt_id,
        stored,
    ) == (task.project_id, task.story_id, task.repository_id, task.planning_attempt_id, target)


async def _holds_work_authority(story, project, runs, task, run, db) -> bool:
    """Whether this receipt is the current, admitted, unstopped attempt that may mint work."""
    if (
        story is None
        or not _owned_attempt(task, run)
        or story.id != task.story_id
        or story.project_id != task.project_id
        or project.id != task.project_id
        or task.status not in _ACTIVE_SOURCE_STATUSES
        or not task.dispatch_admitted
        or story.status != StoryStatus.IN_PROGRESS.value
        or cycle_stamp(run.created_at) < cycle_stamp(story.reopened_at or story.created_at)
    ):
        return False
    if any(
        other.id != run.id
        and not (other.run_metadata or {}).get("pre_handoff_aborted")
        and (
            other.status in _LIVE_RUN_STATUSES
            or (other.task_id == task.id and other.created_at > run.created_at)
        )
        for other in runs
    ):
        return False
    return await locked_disposition(story, task, runs, db) is AttemptDisposition.ELIGIBLE


async def _park_refusal(story, task, run, cause: str, db) -> None:
    """Stop the source and its story for a person with the owed notices; the Run stays."""
    from .routers._story_helpers import _do_transition, _record_story_failure
    from .routers._task_helpers import create_status_event, validate_transition

    details = {HANDOFF_REFUSAL_KEY: cause, "run_id": run.id}
    for target in _REVIEW_HOPS[task.status]:
        before = task.status
        validate_transition(before, target)
        task.status = target.value
        await create_status_event(task, before, target, "engineering", details, db)
    failure = StoryFailure(
        code=StoryFailureCode.LOCAL_PACKAGE_HANDOFF_REFUSED,
        source="engineering",
        detail=f"Attempt {run.id} of task {task.id}: {cause}. No INSTALL was created.",
    )
    _do_transition(story, StoryStatus.WAITING_HUMAN_REVIEW)
    _record_story_failure(story, failure, StoryStatus.WAITING_HUMAN_REVIEW)


async def hand_off_package_build(story, roster, project, runs, task, run, db) -> None:
    """Pin a package-build FEATURE's first completed Run commit into its one INSTALL.

    The caller holds the roster/Story/Project/Run locks and commits with the Run outcome.
    A receipt without work authority keeps only that outcome; a current one creates the
    INSTALL and moves the source's open direct dependents behind it, or stops for a person.
    """
    if not await _holds_work_authority(story, project, runs, task, run, db):
        logger.warning("local_package_handoff_not_current", run_id=run.id, task_id=task.id)
        return
    child_id = local_install_task_id(task.id)
    try:
        target = _pinned_target(task, run)
        repository = await db.get(Repository, task.repository_id)
        if (
            repository is None
            or repository.project_id != task.project_id
            or not repository.is_managed
        ):
            raise _Refused("repository_unowned")
        existing = await db.get(Task, child_id)
        if existing is not None and not _is_pinned_child(existing, task, target):
            raise _Refused("install_identity_conflict")
    except _Refused as refusal:
        logger.warning("local_package_handoff_refused", run_id=run.id, cause=str(refusal))
        await _park_refusal(story, task, run, str(refusal), db)
        return
    if existing is not None:
        return
    now = datetime.now(UTC)
    db.add(
        Task(
            id=child_id,
            project_id=task.project_id,
            type=TaskType.INSTALL.value,
            title=f"Install {target.package.name} {target.package.version}"[:500],
            status=TaskStatus.TODO.value,
            priority=task.priority,
            current_iteration=0,
            created_by="engineering",
            repository_id=task.repository_id,
            story_id=task.story_id,
            blocked_by_task_id=task.id,
            install=target.model_dump(mode="json"),
            dispatch_admitted=task.dispatch_admitted,
            planning_attempt_id=task.planning_attempt_id,
            created_at=now,
            updated_at=now,
        )
    )
    # The INSTALL row exists before any dependent points at it.
    await db.flush()
    for member in roster:
        if (
            member.blocked_by_task_id == task.id
            and member.id != child_id
            and member.story_id == task.story_id
            and member.status not in CLOSED_TASK_STATUSES
        ):
            member.blocked_by_task_id = child_id
    logger.info("local_package_handoff_created", run_id=run.id, task_id=child_id)


async def require_package_handoff(task: Task, db) -> None:
    """Refuse a package-build FEATURE's `done` unless its owned Run's pinned INSTALL exists.

    Called under the source Task lock, which the INSTALL's creator also holds.
    """
    if task.package_build is None:
        return
    child = await db.get(Task, local_install_task_id(task.id))
    if child is not None:
        runs = await db.scalars(
            select(Run).where(Run.task_id == task.id, Run.status == RunStatus.COMPLETED.value)
        )
        for run in runs:
            try:
                if _owned_attempt(task, run) and _is_pinned_child(
                    child, task, _pinned_target(task, run)
                ):
                    return
            except _Refused:
                continue
    raise HTTPException(409, {"code": "local_package_handoff_required"})
