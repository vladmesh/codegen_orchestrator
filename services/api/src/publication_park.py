"""Park the known commit in the same locked write that records its evidence."""

from fastapi import HTTPException
from sqlalchemy import select

from shared.contracts.dto.commit_publication import COMMIT_PUBLICATION_KEY
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.story_failure import StoryFailure, StoryFailureCode
from shared.contracts.dto.task import TaskStatus
from shared.models import Project, Repository


async def park_publication(story, task, run, publication, db):
    from .routers._story_helpers import _do_transition, _record_story_failure
    from .routers._task_helpers import create_status_event, validate_transition

    project = await db.scalar(select(Project).where(Project.id == run.project_id).with_for_update())
    if project is None:
        raise RuntimeError("Publication refusal has no owned Project")
    if publication.attempt_id != run.id or publication.worker_id != (run.run_metadata or {}).get(
        "worker_id"
    ):
        raise HTTPException(409, {"code": "ownership_missing"})
    if publication.repository_id is None:
        if task is not None and task.repository_id:
            repository = await db.get(Repository, task.repository_id)
        else:
            repositories = list(
                (
                    await db.scalars(
                        select(Repository).where(
                            Repository.project_id == project.id, Repository.role == "primary"
                        )
                    )
                ).all()
            )
            if len(repositories) != 1:
                raise HTTPException(409, {"code": "ownership_missing"})
            repository = repositories[0]
        if repository is None or repository.project_id != project.id:
            raise HTTPException(409, {"code": "ownership_missing"})
        publication = publication.model_copy(
            update={
                "repository_id": repository.id,
                "repository_url": repository.git_url,
            }
        )
    evidence = publication.model_dump(mode="json")
    project.config = {**(project.config or {}), COMMIT_PUBLICATION_KEY: evidence}
    run.run_metadata = {**(run.run_metadata or {}), COMMIT_PUBLICATION_KEY: evidence}
    if task is not None:
        task.failure_metadata = {**(task.failure_metadata or {}), COMMIT_PUBLICATION_KEY: evidence}
        if task.status in {TaskStatus.BACKLOG.value, TaskStatus.TODO.value}:
            # Retain the native audited hops, never spend another iteration.
            hops = [TaskStatus.TODO] if task.status == TaskStatus.BACKLOG.value else []
            hops += [TaskStatus.IN_DEV, TaskStatus.WAITING_HUMAN_REVIEW]
        elif task.status in {TaskStatus.IN_DEV.value, TaskStatus.FAILED.value}:
            hops = [TaskStatus.WAITING_HUMAN_REVIEW]
        else:
            hops = []
        for target in hops:
            validate_transition(task.status, target)
            old = task.status
            task.status = target.value
            await create_status_event(
                task,
                old,
                target,
                "publication-owner",
                {
                    COMMIT_PUBLICATION_KEY: evidence,
                    "run_id": run.id,
                },
                db,
            )
    if story is not None:
        if story.status != StoryStatus.WAITING_HUMAN_REVIEW.value:
            failure = StoryFailure(
                code=StoryFailureCode.WORKER_COMMIT_NOT_PUBLISHED,
                source="engineering",
                detail=f"Attempt {run.id}: {publication.failure.value}. "
                "Preserved work requires explicit publication recovery.",
                commit_publication=publication,
            )
            _do_transition(story, StoryStatus.WAITING_HUMAN_REVIEW)
            _record_story_failure(story, failure, StoryStatus.WAITING_HUMAN_REVIEW)
        story.quarantine_reason = {
            **(story.quarantine_reason or {}),
            COMMIT_PUBLICATION_KEY: evidence,
        }
