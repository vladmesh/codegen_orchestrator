"""Server-owned disposition over locked Story/Task/Run facts.

The only precedence is stop, unpublished commit, ordinary eligibility. Callers
must descend Task roster -> Story -> Project -> Run before authorizing work.
"""

from fastapi import HTTPException
from sqlalchemy import select

from shared.contracts.dto.commit_publication import (
    COMMIT_PUBLICATION_KEY,
    AttemptDisposition,
    EngineeringStop,
)
from shared.contracts.dto.run import RunType
from shared.contracts.dto.run_result import EngineeringFailureReason, EngineeringRunResult
from shared.contracts.dto.story import StoryStatus
from shared.models import Project, Run, Story, Task


def disposition(story, task, runs, recovered_attempts=frozenset()):
    if story is not None:
        if story.status in {
            StoryStatus.WAITING_HUMAN_REVIEW.value,
            StoryStatus.FAILED.value,
            StoryStatus.ARCHIVED.value,
            StoryStatus.COMPLETED.value,
        }:
            return AttemptDisposition.STOPPED
        if story.engineering_stop is not None:
            stop = EngineeringStop.model_validate(story.engineering_stop)
            if stop.released_at is None:
                return AttemptDisposition.STOPPED
    if task is not None and COMMIT_PUBLICATION_KEY in (task.failure_metadata or {}):
        return AttemptDisposition.PUBLICATION_REQUIRED
    for run in runs:
        if run.id in recovered_attempts:
            continue
        if COMMIT_PUBLICATION_KEY in (run.run_metadata or {}):
            return AttemptDisposition.PUBLICATION_REQUIRED
        if run.result is None:
            continue
        result = EngineeringRunResult.model_validate(run.result)
        if result.failure_reason is EngineeringFailureReason.WORKER_COMMIT_NOT_PUBLISHED:
            return AttemptDisposition.PUBLICATION_REQUIRED
    return AttemptDisposition.ELIGIBLE


async def lock_story_attempts(story_id, db):
    """Column-only discovery, ascending roster, Story, Project, ascending Runs."""
    from .routers._story_helpers import _get_story_for_update
    from .routers._task_helpers import get_task_for_update

    ids = list((await db.scalars(select(Task.id).where(Task.story_id == story_id))).all())
    tasks = [await get_task_for_update(i, db) for i in sorted(ids)]
    story = await _get_story_for_update(story_id, db)
    current = set((await db.scalars(select(Task.id).where(Task.story_id == story_id))).all())
    if current - set(ids):
        raise HTTPException(409, {"code": "story_roster_changed"})
    project = await db.scalar(
        select(Project).where(Project.id == story.project_id).with_for_update()
    )
    runs = list(
        (
            await db.scalars(
                select(Run)
                .where(Run.story_id == story_id, Run.type == RunType.ENGINEERING.value)
                .order_by(Run.id)
                .with_for_update()
            )
        ).all()
    )
    return story, tasks, project, runs


async def recovered_attempts(db, runs):
    from shared.models.commit_recovery import CommitRecovery

    return set(
        (
            await db.scalars(
                select(CommitRecovery.attempt_id).where(
                    CommitRecovery.attempt_id.in_([r.id for r in runs]),
                    CommitRecovery.handed_off_at.is_not(None),
                )
            )
        ).all()
    )


async def locked_disposition(story, task, runs, db):
    preliminary = disposition(story, task, runs)
    if preliminary is not AttemptDisposition.PUBLICATION_REQUIRED:
        return preliminary
    return disposition(story, task, runs, await recovered_attempts(db, runs))


async def require_automatic_task(task, db):
    """Called after the Task lock; shared by automatic retry/start/completion."""
    story = None
    if task.story_id:
        # All writers that examine siblings use lock_story_attempts instead.
        story = await db.scalar(select(Story).where(Story.id == task.story_id).with_for_update())
        if story is None:
            raise HTTPException(409, "Task Story ownership is missing")
    # The terminal writer/park owner commits Task evidence and Story status
    # together with the Run evidence. These locked rows authorize a Task hop;
    # paid/worker admission additionally examines the attempt rows.
    state = disposition(story, task, [])
    if state is not AttemptDisposition.ELIGIBLE:
        raise HTTPException(409, {"code": "engineering_attempt_fenced", "disposition": state.value})
