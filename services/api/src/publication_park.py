"""Park the known commit in the same locked write that records its evidence."""

from fastapi import HTTPException
from sqlalchemy import select

from shared.contracts.dto.commit_publication import COMMIT_PUBLICATION_KEY, CommitPublication
from shared.contracts.dto.run_result import EngineeringRunResult
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.story_failure import StoryFailure, StoryFailureCode
from shared.contracts.dto.task import TaskStatus
from shared.models import Project, Repository
from shared.models.commit_recovery import CommitRecovery


def publication_projections(value):
    """All publication fields in a quarantine, including typed nested causes."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key == COMMIT_PUBLICATION_KEY and child is not None:
                yield CommitPublication.model_validate(child)
            else:
                yield from publication_projections(child)
    elif isinstance(value, list):
        for child in value:
            yield from publication_projections(child)


async def active_publications(runs, db):
    """Canonical attempts/claims authorize projections, never the reverse."""
    from .attempt_disposition import recovered_attempts

    retired = await recovered_attempts(db, runs)
    active = {}
    for run in runs:
        if run.id in retired:
            continue
        raw = (run.run_metadata or {}).get(COMMIT_PUBLICATION_KEY)
        if raw is not None:
            active[run.id] = CommitPublication.model_validate(raw)
        elif run.result is not None:
            result = EngineeringRunResult.model_validate(run.result)
            if result.publication is not None:
                active[run.id] = result.publication
    return active


async def guard_publication_failure(failure, runs, db):
    if failure is None or failure.commit_publication is None:
        return
    publication = failure.commit_publication
    active = await active_publications(runs, db)
    if active.get(publication.attempt_id) != publication:
        raise HTTPException(409, {"code": "stale_attempt"})


async def guard_quarantine_patch(story, proposed, runs, db):
    if proposed == story.quarantine_reason:
        return
    if await active_publications(runs, db) or list(publication_projections(proposed)):
        raise HTTPException(409, {"code": "commit_publication_required"})


def retired_publication_controls(story, task, project, run, claim):
    """Validate every control before releasing anything, then retire exact proof.

    The caller owns the roster/Story/Project/Run/claim lock ladder and commits
    these prepared values with stop release, native completion and handed_off_at.
    Original Run/ledger and claim/receipt remain the diagnostic history.
    """
    receipt = CommitPublication.model_validate(claim.receipt)
    if (
        claim.attempt_id != run.id
        or not receipt.published
        or receipt.attempt_id != run.id
        or receipt.commit_sha != claim.commit_sha
        or receipt.branch != claim.identity["branch"]
        or receipt.worker_id != claim.identity["worker_id"]
    ):
        raise HTTPException(409, {"code": "ownership_missing"})

    def retire(value):
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                if key == COMMIT_PUBLICATION_KEY and child is not None:
                    held = CommitPublication.model_validate(child)
                    bound_fields = (
                        (held.commit_sha, claim.commit_sha),
                        (held.worker_id, claim.identity["worker_id"]),
                        (held.branch, claim.identity["branch"]),
                        (held.repository_id, claim.identity["repository_id"]),
                        (held.repository_url, claim.identity["repository_url"]),
                    )
                    if held.attempt_id != run.id or any(
                        saved is not None and saved != proven for saved, proven in bound_fields
                    ):
                        raise HTTPException(409, {"code": "stale_attempt"})
                    continue
                result[key] = retire(child)
            return result
        if isinstance(value, list):
            return [retire(child) for child in value]
        return value

    # Project config contains unrelated policy/secrets; retire only its owned
    # checkout hold. Story/Task causes may contain nested typed projections.
    config = dict(project.config or {})
    hold = config.get(COMMIT_PUBLICATION_KEY)
    if hold is not None:
        retire({COMMIT_PUBLICATION_KEY: hold})
        config.pop(COMMIT_PUBLICATION_KEY)
    reason = retire(story.quarantine_reason)
    metadata = retire(task.failure_metadata) if task is not None else None
    return reason, metadata, config


async def park_publication(story, task, run, publication, db):
    from .routers._story_helpers import _do_transition, _record_story_failure
    from .routers._task_helpers import create_status_event, validate_transition

    if publication.attempt_id != run.id or publication.worker_id != (run.run_metadata or {}).get(
        "worker_id"
    ):
        raise HTTPException(409, {"code": "ownership_missing"})
    claim = await db.get(CommitRecovery, run.id)
    if claim is not None and claim.handed_off_at is not None:
        return  # Every producer, including stop reconciliation, reaches this fence.
    project = await db.scalar(select(Project).where(Project.id == run.project_id).with_for_update())
    if project is None:
        raise RuntimeError("Publication refusal has no owned Project")
    existing = list(publication_projections(story.quarantine_reason)) if story is not None else []
    existing += list(publication_projections(task.failure_metadata)) if task is not None else []
    existing += list(
        publication_projections(
            {COMMIT_PUBLICATION_KEY: (project.config or {}).get(COMMIT_PUBLICATION_KEY)}
        )
    )
    if any(held.attempt_id != run.id for held in existing):
        raise HTTPException(409, {"code": "stale_attempt"})
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
