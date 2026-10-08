"""Explicit operator recovery of retained mechanical work, never coding work."""

import re

from fastapi import HTTPException
from sqlalchemy import select
import structlog

from shared.clients.github import GitHubAppClient
from shared.contracts.dto.catalog_install import InstallDecision, InstallOperation
from shared.contracts.dto.commit_publication import EngineeringStop
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskEventType, TaskStatus
from shared.contracts.queues.architect import ArchitectMessage
from shared.models import Repository, TaskEvent
from shared.queues import ARCHITECT_QUEUE

from .attempt_disposition import release_engineering_stop
from .engineering_dispatch_admission import _lock_dispatch_tasks

logger = structlog.get_logger()

#: The detail prefix of the scheduler's park after an install PR's CI failed
#: (`refuse_install_coding_fallback`); the only stop `replan` may release.
INSTALL_CI_REVIEW_PREFIX = "Catalog installation requires review:"

#: What `replan` answers when its commit landed but the architect job did not.
ARCHITECT_PUBLISH_FAILED = "architect_publish_failed"


def _github_repository(repository):
    url = repository.git_url.removesuffix(".git").rstrip("/")
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", url):
        raise HTTPException(409, {"code": "repository_unowned"})
    return url.split("/")[-2:]


async def operator_install_recovery(task_id, body, actor, db, redis=None):
    from .routers._story_helpers import _do_transition, _get_story_for_update
    from .routers._task_helpers import create_status_event, validate_transition
    from .routers.projects_guards import load_locked_project

    task, _, _, story_id = await _lock_dispatch_tasks(task_id, db)
    if task.type != "install" or task.install_operation is None:
        raise HTTPException(409, {"code": "install_operation_missing"})
    operation = InstallOperation.model_validate(task.install_operation)
    story = await _get_story_for_update(story_id, db)
    await load_locked_project(db, task.project_id)
    repository = await db.scalar(
        select(Repository).where(Repository.id == task.repository_id).with_for_update()
    )
    cause = story.quarantine_reason
    if body.action == "replan":
        return await _replan(task, operation, story, repository, body, actor, db, redis)
    owns_review = (
        story.status == StoryStatus.WAITING_HUMAN_REVIEW.value
        and cause
        and cause.get("detail", "").startswith(f"Catalog install {operation.id} at ")
        and operation.cycle_started_at == (story.reopened_at or story.created_at)
    )
    if (
        task.status == TaskStatus.CANCELLED.value
        and not owns_review
        and operation.id == body.operation_id
        and operation.state in {"refused", "recovery_required"}
        and body.action == "retry"
        and body.stop_id is None
    ):
        # Reconciliation of a cancelled writer releases only that operation.
        # A newer cycle or another stop remains owned by its existing authority.
        db.add(
            TaskEvent(
                task_id=task.id,
                event_type=TaskEventType.NOTE.value,
                actor=actor,
                details={
                    "catalog_install_settlement": operation.model_dump(mode="json"),
                    "operator_action": body.action,
                },
            )
        )
        operation.state = "refused"
        task.install_operation = operation.model_dump(mode="json")
        await db.commit()
        return InstallDecision(outcome="settled", operation=operation)
    if (
        operation.id != body.operation_id
        or operation.cycle_started_at != (story.reopened_at or story.created_at)
        or task.status not in {TaskStatus.WAITING_HUMAN_REVIEW.value, TaskStatus.CANCELLED.value}
        or story.status != StoryStatus.WAITING_HUMAN_REVIEW.value
        or operation.state not in {"refused", "recovery_required"}
    ):
        raise HTTPException(409, {"code": "stale_install_recovery"})
    if not cause or not cause.get("detail", "").startswith(f"Catalog install {operation.id} at "):
        raise HTTPException(409, {"code": "unrelated_story_stop"})
    if body.action == "recover":
        if task.status == TaskStatus.CANCELLED.value:
            raise HTTPException(409, {"code": "cancelled_install_not_recoverable"})
        if operation.head_sha is None or operation.verification is None:
            raise HTTPException(409, {"code": "install_verification_missing"})
        owner, name = _github_repository(repository)
        async with GitHubAppClient() as github:
            head = await github.get_ref_sha(owner, name, f"heads/story/{story.id}")
        if head != operation.head_sha:
            raise HTTPException(
                409,
                {
                    "code": "install_head_not_published",
                    "detail": "Publish the reviewed saved commit without force, then recover.",
                },
            )
    # Only this selected operation's matching stop may be released. API auth
    # supplies actor; request text cannot grant operator authority.
    release_engineering_stop(story, body.stop_id, actor, db, expected_cause=cause)
    db.add(
        TaskEvent(
            task_id=task.id,
            event_type=TaskEventType.NOTE.value,
            actor=actor,
            details={
                "catalog_install_settlement": operation.model_dump(mode="json"),
                "operator_action": body.action,
            },
        )
    )

    async def move(status):
        before = task.status
        validate_transition(before, status)
        task.status = status.value
        await create_status_event(task, before, status, actor, {}, db)

    if body.action == "recover":
        await move(TaskStatus.IN_DEV)
        for status in (TaskStatus.IN_CI, TaskStatus.TESTING, TaskStatus.DONE):
            await move(status)
        operation.state, operation.stage = "published", "published"
        task.install_operation = operation.model_dump(mode="json")
    elif task.status == TaskStatus.CANCELLED.value:
        operation.state = "refused"
        task.install_operation = operation.model_dump(mode="json")
    else:
        await move(TaskStatus.BACKLOG)
        await move(TaskStatus.TODO)
        task.install_operation = None
    story.quarantine_reason = None
    _do_transition(story, StoryStatus.IN_PROGRESS)
    await db.commit()
    return InstallDecision(outcome="settled", operation=operation)


async def _replan(task, operation, story, repository, body, actor, db, redis):  # noqa: PLR0913  # one locked ladder's rows
    """Send a story parked after its install PR's CI failed back to the architect.

    The install Task pinned its package release at planning, so re-running it
    cannot pick up a fixed release: a new attempt needs a new plan against the
    current catalog. The settled Task is cancelled out of the work cycle, the
    story walks waiting_human_review -> failed -> reopened, and after the commit
    the architect is asked to re-plan exactly as a reopen asks it. Nothing on
    GitHub is written here: the operator closes the PR and deletes the branch.
    """
    from .routers._recipients import resolve_project_chat_id
    from .routers._story_actions import COMPOSITE_CHAINS, REPLAN_CATALOG_INSTALL, _apply_chain
    from .routers._task_helpers import apply_cancellation, create_status_event, validate_transition

    if redis is None:
        raise RuntimeError("replan publishes the architect job and needs the queue client")
    cause = story.quarantine_reason
    cycle = story.reopened_at or story.created_at
    if operation.id != body.operation_id:
        raise HTTPException(409, {"code": "stale_install_operation"})
    if operation.state != "published":
        raise HTTPException(409, {"code": "install_operation_not_published"})
    if task.status != TaskStatus.DONE.value:
        raise HTTPException(409, {"code": "install_task_not_done"})
    if story.status != StoryStatus.WAITING_HUMAN_REVIEW.value:
        raise HTTPException(409, {"code": "story_not_waiting_human_review"})
    if (
        not cause
        or cause.get("source") != "scheduler"
        or not cause.get("detail", "").startswith(INSTALL_CI_REVIEW_PREFIX)
    ):
        raise HTTPException(409, {"code": "unrelated_story_stop"})
    stop = (
        EngineeringStop.model_validate(story.engineering_stop) if story.engineering_stop else None
    )
    if stop is None or stop.released_at is not None or stop.id != body.stop_id:
        raise HTTPException(409, {"code": "engineering_stop_mismatch"})
    if operation.cycle_started_at != cycle:
        raise HTTPException(409, {"code": "stale_install_cycle"})
    owner, name = _github_repository(repository)
    async with GitHubAppClient() as github:
        head = await github.get_ref_sha(owner, name, f"heads/story/{story.id}")
    if head is not None:
        raise HTTPException(
            409,
            {
                "code": "install_branch_present",
                "detail": f"Close the install PR and delete the remote branch story/{story.id}, "
                "then replan.",
            },
        )

    release_engineering_stop(story, body.stop_id, actor, db, expected_cause=cause)
    db.add(
        TaskEvent(
            task_id=task.id,
            event_type=TaskEventType.NOTE.value,
            actor=actor,
            details={
                "catalog_install_settlement": operation.model_dump(mode="json"),
                "operator_action": body.action,
            },
        )
    )
    # done -> cancelled is not a hop of its own; backlog is the only way out of
    # done, and cancellation then takes the one writer every cancel uses.
    validate_transition(task.status, TaskStatus.BACKLOG)
    before = task.status
    task.status = TaskStatus.BACKLOG.value
    await create_status_event(task, before, TaskStatus.BACKLOG, actor, {}, db)
    await apply_cancellation(task, db)
    story.quarantine_reason = None
    _apply_chain(story, COMPOSITE_CHAINS[REPLAN_CATALOG_INSTALL])
    message = ArchitectMessage(
        story_id=story.id,
        project_id=str(story.project_id),
        telegram_chat_id=await resolve_project_chat_id(
            db, story.project_id, event="catalog_install_replan", story_id=story.id
        ),
        is_reopen=True,
    )
    await db.commit()

    try:
        await redis.publish_message(ARCHITECT_QUEUE, message)
    except Exception:
        # The story is reopened and the stop released, so a repeat call answers
        # 409 and cannot publish again. The operator re-sends the job through
        # `POST /api/stories/{id}/send-to-architect`, whose owed record the
        # scheduler publishes.
        logger.exception(
            "catalog_install_replan_publish_failed", story_id=message.story_id, task_id=task.id
        )
        return InstallDecision(
            outcome="settled", reason=ARCHITECT_PUBLISH_FAILED, operation=operation
        )
    logger.info("catalog_install_replanned", story_id=message.story_id, task_id=task.id)
    return InstallDecision(outcome="settled", operation=operation)
