"""Durable catalog installation admission and settlement, without a paid Run."""

from datetime import UTC, datetime, timedelta
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from shared.contracts.dto.catalog_install import (
    GLUE_REPAIR_AUTHOR,
    SETTLEMENT_NOTE_KEY,
    CatalogInstall,
    InstallCommand,
    InstallDecision,
    InstallOperation,
    product_glue_handoff,
)
from shared.contracts.dto.commit_publication import AttemptDisposition
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.story_failure import StoryFailure, StoryFailureCode
from shared.contracts.dto.task import TaskEventType, TaskStatus, TaskType
from shared.diagnostics import redact_diagnostic
from shared.models import Repository, Run, Task, TaskEvent

from .attempt_disposition import locked_disposition
from .engineering_dispatch_admission import _lock_dispatch_tasks, _take_story_roster

INSTALL_LEASE = timedelta(minutes=15)


def _glue_repair(task, operation, stage, payload, locked, cycle) -> str | None:  # noqa: PLR0913  # one locked refusal's rows
    """The repair a glue refusal hands to engineering, once, or `None` to park it.

    Only an untouched attempt of the current cycle whose answer is product glue is
    repaired, and only once: a second glue answer after a repair is a person's.
    """
    preflight = operation.preflight
    if (
        preflight is None
        or stage != "preflight"
        or operation.head_sha is not None
        or operation.cycle_started_at != cycle
        or task.status != TaskStatus.IN_DEV.value
    ):
        return None
    previous = locked.get(task.blocked_by_task_id) if task.blocked_by_task_id else None
    if previous is not None and previous.created_by == GLUE_REPAIR_AUTHOR:
        return None
    return product_glue_handoff(preflight, payload)


async def install_command(task_id, command: InstallCommand, db) -> InstallDecision:  # noqa: C901, PLR0911, PLR0912, PLR0915  # finite owned settlement under one lock ladder
    from .routers._story_helpers import _do_transition, _get_story_for_update, _record_story_failure
    from .routers._task_helpers import create_status_event, generate_id, validate_transition
    from .routers.projects_guards import load_locked_project

    task, locked, _, story_id = await _lock_dispatch_tasks(task_id, db)
    if task.type != TaskType.INSTALL.value or not task.story_id or not task.repository_id:
        raise HTTPException(409, {"code": "not_catalog_install"})
    payload = CatalogInstall.model_validate(task.install)
    _, reason = await _take_story_roster(task, story_id, locked, db)
    story = await _get_story_for_update(task.story_id, db)
    project = await load_locked_project(db, task.project_id)
    repository = await db.scalar(
        select(Repository).where(Repository.id == task.repository_id).with_for_update()
    )
    if repository is None or repository.project_id != task.project_id or not repository.is_managed:
        raise HTTPException(409, {"code": "repository_unowned"})
    operation = (
        InstallOperation.model_validate(task.install_operation) if task.install_operation else None
    )
    now = datetime.now(UTC)
    cycle = story.reopened_at or story.created_at
    cycle = cycle.replace(tzinfo=UTC)

    async def move(status):
        before = task.status
        validate_transition(before, status)
        task.status = status
        await create_status_event(task, before, status, "catalog_install", {}, db)

    async def save(outcome):
        task.install_operation = operation.model_dump(mode="json")
        await db.commit()
        return InstallDecision(
            outcome=outcome,
            operation=operation,
            install=payload,
            project_name=repository.name,
            git_url=repository.git_url,
        )

    def refuse(code):
        return InstallDecision(outcome="refused", reason=code, operation=operation)

    async def park(stage, detail, state="recovery_required", *, park_story=True):
        operation.state = state
        operation.stage = stage
        operation.detail = redact_diagnostic(detail)[:2000]
        if task.status == TaskStatus.TODO.value:
            await move(TaskStatus.IN_DEV)
        if task.status == TaskStatus.IN_DEV.value:
            await move(TaskStatus.WAITING_HUMAN_REVIEW)
        if park_story and story.status == StoryStatus.IN_PROGRESS.value:
            failure = StoryFailure(
                code=StoryFailureCode.SCAFFOLD_FAILED,
                source="scaffolder",
                detail=f"Catalog install {operation.id} at {stage}: {operation.detail}",
            )
            _record_story_failure(story, failure, StoryStatus.WAITING_HUMAN_REVIEW)
            _do_transition(story, StoryStatus.WAITING_HUMAN_REVIEW)
        return await save("settled")

    async def holds_work_authority():
        """Whether this refusing owner may still create work, not only settle evidence.

        New engineering work needs what admission needed: the live execution lease, the
        current cycle's story in progress with no stop (`_take_story_roster`), a
        dispatch-admitted task and an eligible attempt disposition. A stopped, cancelled
        or expired owner keeps its typed refusal on the operation and nothing else.
        """
        if (
            reason is not None
            or story.status != StoryStatus.IN_PROGRESS.value
            or not task.dispatch_admitted
            or operation.heartbeat_at is None
            or operation.heartbeat_at + INSTALL_LEASE < now
        ):
            return False
        runs = list(
            (
                await db.scalars(
                    select(Run)
                    .where(Run.project_id == project.id, Run.type == RunType.ENGINEERING.value)
                    .order_by(Run.id)
                    .with_for_update()
                )
            ).all()
        )
        return await locked_disposition(story, task, runs, db) is AttemptDisposition.ELIGIBLE

    async def hand_off_glue(description):
        """One concrete product repair, then this same INSTALL again, on the repaired head.

        Nothing was installed: the operation settles as refused at preflight, its typed
        answer is kept as the task's settlement note, and the task waits behind the
        repair. Its coverage and the work chained after it are unchanged, so the module
        still precedes the rest of the product glue. The next admission is a new
        operation in a new checkout, and the kit decides again.
        """
        operation.state, operation.stage = "refused", "preflight"
        operation.detail = redact_diagnostic(command.detail or "glue_required")[:2000]
        repair = Task(
            id=generate_id(),
            project_id=task.project_id,
            type=TaskType.FIX.value,
            title=f"Product glue before installing {payload.package.name}"[:255],
            description=description,
            status=TaskStatus.TODO.value,
            priority=task.priority,
            acceptance_criteria=(
                "The kit's read-only check-install of the same release reports no product "
                "conflict on the repaired story head."
            ),
            current_iteration=0,
            max_iterations=3,
            created_by=GLUE_REPAIR_AUTHOR,
            repository_id=task.repository_id,
            story_id=task.story_id,
            blocked_by_task_id=task.blocked_by_task_id,
            # The confirmed plan already admitted this install; its concrete repair is
            # part of carrying it out, not new work to plan.
            dispatch_admitted=True,
            created_at=now,
            updated_at=now,
        )
        db.add(repair)
        # The repair row exists before the install points at it.
        await db.flush()
        db.add(
            TaskEvent(
                task_id=task.id,
                event_type=TaskEventType.NOTE.value,
                actor="catalog_install",
                details={
                    SETTLEMENT_NOTE_KEY: operation.model_dump(mode="json"),
                    "glue_repair_task_id": repair.id,
                },
            )
        )
        for status in (TaskStatus.BLOCKED, TaskStatus.BACKLOG, TaskStatus.TODO):
            await move(status)
        task.blocked_by_task_id = repair.id
        task.install_operation = None
        await db.commit()
        return InstallDecision(outcome="settled", operation=operation, install=payload)

    if operation is not None:
        if command.action != "admit" and command.operation_id != operation.id:
            return refuse("stale_operation")
        # Cancellation/stop may forbid more work while the same owner still owes
        # a refusal record. It must retain exact head/workspace evidence without
        # changing a newer cycle, cancelled task or an unrelated story stop.
        if (
            command.action == "refuse"
            and operation.state == "running"
            and operation.token == command.token
        ):
            if command.head_sha:
                operation.head_sha = command.head_sha
            if command.preflight is not None:
                # The kit's typed answer — glue, incompatible or untrusted — stays with
                # the operation it refused, for the handoff and the person who reviews it.
                operation.preflight = command.preflight
            stage = command.stage or operation.stage
            repair = _glue_repair(task, operation, stage, payload, locked, cycle)
            if repair is not None and await holds_work_authority():
                return await hand_off_glue(repair)
            return await park(
                stage,
                command.detail or "Install refused.",
                "recovery_required"
                if operation.head_sha or stage not in {"claimed", "prepare", "preflight"}
                else "refused",
                park_story=operation.cycle_started_at == cycle,
            )

        if operation.state == "running" and (
            operation.heartbeat_at is None or operation.heartbeat_at + INSTALL_LEASE < now
        ):
            return await park(
                "lease_lost",
                "Execution lease expired; inspect retained checkout and exact head before retry.",
                park_story=operation.cycle_started_at == cycle,
            )
        if operation.cycle_started_at != cycle:
            return refuse("stale_cycle")
        if operation.state in {"published", "refused", "recovery_required"}:
            return InstallDecision(outcome="settled", operation=operation)

    if reason is not None or story.status != StoryStatus.IN_PROGRESS.value:
        return refuse(reason.value if reason else "story_not_in_progress")
    if not task.dispatch_admitted:
        return refuse("product_brief_not_admitted")
    if task.blocked_by_task_id and (
        task.blocked_by_task_id not in locked
        or locked[task.blocked_by_task_id].status != TaskStatus.DONE.value
    ):
        return refuse("blocker_unresolved")
    if task.status not in {TaskStatus.TODO.value, TaskStatus.IN_DEV.value}:
        return refuse("task_not_dispatchable")
    if project.status != "active" or (project.config or {}).get("workspace_ready") is not True:
        return refuse("workspace_not_ready")
    if story.project_id != task.project_id:
        return refuse("story_unowned")
    for member in locked.values():
        if member.id == task.id:
            continue
        if member.install_operation and member.install_operation["state"] in {
            "queued",
            "running",
            "recovery_required",
        }:
            return refuse("branch_writer_live")
        if member.story_id == story.id and member.status in {"in_dev", "waiting_human_review"}:
            return refuse("branch_writer_live")
    runs = list(
        (
            await db.scalars(
                select(Run)
                .where(Run.project_id == project.id, Run.type == RunType.ENGINEERING.value)
                .order_by(Run.id)
                .with_for_update()
            )
        ).all()
    )
    if any(run.status in {RunStatus.QUEUED.value, RunStatus.RUNNING.value} for run in runs):
        return refuse("branch_writer_live")
    authority = await locked_disposition(story, task, runs, db)
    if authority is not AttemptDisposition.ELIGIBLE:
        return refuse(authority.value)
    if operation and operation.state == "running" and command.action == "admit":
        return refuse("operation_live")
    if command.action == "admit":
        if payload.catalog is None:
            # Stored before installs named their catalog commit: `kit add` would read the
            # kit's moving default branch. An operator replans it against the snapshot.
            return refuse("catalog_unpinned")
        if operation is None:
            operation = InstallOperation(
                id=f"install-{uuid.uuid4().hex}",
                project_id=task.project_id,
                task_id=task.id,
                story_id=story.id,
                repository_id=repository.id,
                cycle_started_at=cycle,
                state="queued",
                stage="queued",
            )
        return await save("admitted")
    if operation is None:
        return refuse("operation_not_admitted")
    if command.action == "claim":
        if not command.token:
            raise HTTPException(422, {"code": "claim_token_required"})
        if operation.state == "running":
            if operation.token == command.token:
                return await save("claimed")
            return refuse("operation_live")
        operation.state, operation.stage = "running", "claimed"
        operation.token, operation.heartbeat_at = command.token, now
        await move(TaskStatus.IN_DEV)
        return await save("claimed")
    if operation.token != command.token or operation.state != "running":
        return refuse("lease_lost")
    if operation.heartbeat_at is None or operation.heartbeat_at + INSTALL_LEASE < now:
        return await park("lease_lost", "Execution lease expired; retained work needs review.")
    operation.heartbeat_at = now
    if command.verification is not None:
        proof = command.verification
        expected = {item.name: item.version for item in [payload.package, *payload.libraries]}
        if (
            proof.core_version != payload.core_version
            or proof.tooling_commit != payload.tooling_commit
            or proof.binding_sha256 != payload.binding.sha256
            or proof.distributions != expected
            or set(proof.component_targets) != set(expected)
        ):
            return refuse("verification_mismatch")
        if operation.verification is not None and operation.verification != proof:
            return refuse("verification_changed")
        operation.verification = proof
    if command.action == "checkpoint":
        operation.stage = command.stage or operation.stage
        if command.checkout:
            # The one attempt checkout this operation may run in is derived from it.
            if command.checkout != f"{operation.repository_id}/{operation.id}":
                return refuse("checkout_unowned")
            operation.checkout = command.checkout
        if command.preflight is not None:
            if operation.preflight is not None and operation.preflight != command.preflight:
                return refuse("preflight_changed")
            if command.preflight.provenance_mismatch(payload) is not None:
                return refuse("preflight_provenance_mismatch")
            operation.preflight = command.preflight
        if command.base_sha:
            if operation.base_sha and operation.base_sha != command.base_sha:
                return refuse("base_changed")
            operation.base_sha = command.base_sha
        if command.head_sha:
            if operation.head_sha and operation.head_sha != command.head_sha:
                return refuse("head_changed")
            operation.head_sha = command.head_sha
    if command.action == "publish":
        if (
            not operation.head_sha
            or operation.head_sha != command.head_sha
            or operation.verification is None
        ):
            return refuse("head_unverified")
        if (
            operation.preflight is None
            or operation.preflight.status == "incompatible"
            or operation.preflight.outstanding_glue(payload)
        ):
            # Only a release the kit admitted on this product, with no product glue
            # outstanding, is an installation; anything else was never installed.
            return refuse("preflight_unverified")
        operation.state, operation.stage = "published", "published"
        for status in (TaskStatus.IN_CI, TaskStatus.TESTING, TaskStatus.DONE):
            await move(status)
    return await save("settled" if command.action == "publish" else "reused")
