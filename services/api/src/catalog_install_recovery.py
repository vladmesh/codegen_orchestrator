"""Explicit operator recovery of retained mechanical work, never coding work."""

import re

from fastapi import HTTPException
from sqlalchemy import select

from shared.clients.github import GitHubAppClient
from shared.contracts.dto.catalog_install import InstallDecision, InstallOperation
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskEventType, TaskStatus
from shared.models import Repository, TaskEvent

from .attempt_disposition import release_engineering_stop
from .engineering_dispatch_admission import _lock_dispatch_tasks


async def operator_install_recovery(task_id, body, actor, db):
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
        url = repository.git_url.removesuffix(".git").rstrip("/")
        if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", url):
            raise HTTPException(409, {"code": "repository_unowned"})
        owner, name = url.split("/")[-2:]
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
