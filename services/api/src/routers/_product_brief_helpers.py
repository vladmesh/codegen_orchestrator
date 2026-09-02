"""The Product Brief row reader and the planning fence every writer shares.

Two routers need the brief: `product_briefs.py`, which owns the claim, the
coverage writes and the one admission step, and `tasks.py`, which has to know
whether the task it is about to create belongs to a plan that has not been
admitted yet, and whether the task it is about to update may change the plan it
belongs to. Both take the brief the same way — `SELECT ... FOR UPDATE` — and
both ask the planning fence the same question, so there is one answer to "may
this caller act as the planner of this brief" rather than two.

**Lock order.** Every path here takes the brief row *before* any Task row. The
declared dispatch admission point takes Task rows first and never reads a brief
at all (see `engineering_dispatch_admission.LOCK_LADDER`), so brief-before-task
adds no cycle: no transaction in this service holds a Task row and then waits
for a brief.
"""

from datetime import UTC, datetime, timedelta
import uuid

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.product_brief import PLANNING_ATTEMPT_HEARTBEAT_TIMEOUT_SECONDS
from shared.models import ProductBrief, RequirementCoverage, Task

from ._task_helpers import apply_cancellation, cancellation_is_reachable, get_task_for_update


async def load_brief_for_update(brief_id: str, db: AsyncSession) -> ProductBrief:
    """Take one brief row for update, or 404. The only reader of a brief that decides."""
    brief = (
        await db.execute(select(ProductBrief).where(ProductBrief.id == brief_id).with_for_update())
    ).scalar_one_or_none()
    if brief is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Product Brief {brief_id} not found"
        )
    return brief


async def brief_of_story_for_update(story_id: str, db: AsyncSession) -> ProductBrief | None:
    """The brief backing this story, taken for update, or None for ordinary work."""
    return (
        await db.execute(
            select(ProductBrief).where(ProductBrief.story_id == story_id).with_for_update()
        )
    ).scalar_one_or_none()


def attempt_heartbeat_is_fresh(brief: ProductBrief, now: datetime) -> bool:
    """Is the current owner still proving it is alive?

    A claim without a fresh heartbeat is abandonable: the architect that held it
    died, and the plan would otherwise be owned by nobody for ever. A claim with
    one is not, which is what makes a planning retry report in-progress instead
    of issuing a rival attempt.
    """
    if brief.planning_attempt_heartbeat_at is None:
        return False
    heartbeat = brief.planning_attempt_heartbeat_at
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=UTC)
    return heartbeat >= now - timedelta(seconds=PLANNING_ATTEMPT_HEARTBEAT_TIMEOUT_SECONDS)


def require_active_attempt(brief: ProductBrief, planning_attempt_id: str | None) -> None:
    """Refuse anyone but the architect that currently owns this brief's plan.

    Not a liveness question: a stale owner that has not been taken over is still
    the owner, and an owner that *has* been taken over presents an id the row no
    longer names. Either way the row is the authority, so this decides from the
    row it was handed — which every caller took for update.
    """
    if not brief.planning_attempt_active or brief.planning_attempt_id != planning_attempt_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Product Brief planning requires the currently active planning attempt",
        )


async def plan_admission_for_new_task(
    *,
    project_id: uuid.UUID,
    story_id: str | None,
    planning_attempt_id: str | None,
    db: AsyncSession,
) -> tuple[bool, str | None]:
    """Decide `(dispatch_admitted, planning_attempt_id)` for a task being created.

    True — dispatchable the moment it is `todo` — for everything except a task
    planned into a story whose brief has not been admitted yet. That is the
    whole of the default's safety: a project with no brief, a story with no
    brief, and a story whose brief was already admitted all keep the behaviour
    they have today.
    """
    if story_id is None:
        return True, None
    brief = await brief_of_story_for_update(story_id, db)
    if brief is None:
        return True, None
    if brief.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="a brief-backed task must belong to the Product Brief's project",
        )
    if brief.coverage_admitted_at is not None:
        # The boundary was crossed: this story's plan is released, and work
        # added to it afterwards is ordinary work with an ordinary lifecycle.
        return True, None
    require_active_attempt(brief, planning_attempt_id)
    return False, planning_attempt_id


#: The fields that say which plan a task belongs to. While the task is
#: unadmitted, all three are frozen; `TaskUpdate` forbids extras and does not
#: expose `planning_attempt_id` today, but the fence is written over the
#: membership itself rather than over one schema's current field list.
PLAN_MEMBERSHIP_FIELDS = ("project_id", "story_id", "planning_attempt_id")


async def _refuse_move_into_unadmitted_plan(story_id: str | None, db: AsyncSession) -> None:
    """The inbound half: a task may not be moved into a plan still being built.

    The release set of an admission is exactly the tasks planned under that
    attempt. A task moved in from elsewhere belongs to no attempt, so nothing
    would ever release it — it would sit in the story permanently undispatchable
    while its siblings ran. Refusing says that out loud instead of manufacturing
    stranded work.
    """
    if story_id is None:
        return
    brief = await brief_of_story_for_update(story_id, db)
    if brief is None or brief.coverage_admitted_at is not None:
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            "cannot move a task into a story whose Product Brief coverage is not admitted; "
            "plan it under the active architect planning attempt instead"
        ),
    )


async def take_task_for_plan_fenced_update(
    *, task_id: str, update_data: dict, db: AsyncSession
) -> Task:
    """Take the task an update is about, with both directions of the plan fence.

    *A planned task's plan membership is immutable while it is unadmitted.* One
    guard, both directions, because they are one invariant:

    * inbound — a task may not join a plan that is still being built, because
      nothing would ever release it;
    * outbound — while `dispatch_admitted` is false, its project, story and
      planning attempt are frozen. Moving it out of the story strands it: the
      admission's release set is keyed on story *and* attempt, so the brief
      would stamp `coverage_admitted_at` over work that is never released and
      can never be released again, the attempt being closed by then. Changing
      the project releases the wrong thing: a disposition approved under one
      project's brief would release engineering work charged to another.

    The task is not stuck. Finishing the plan releases it, and a plan that
    should not proceed is abandoned through the planning-attempt fence.

    Returns the task row, locked, so the caller does not take it twice. The
    inbound question is asked *first*, because the brief row sits above every
    Task row in this service's lock order (see the module docstring).
    """
    await _refuse_move_into_unadmitted_plan(update_data.get("story_id"), db)
    task = await get_task_for_update(task_id, db)
    if task.dispatch_admitted:
        return task
    moved = [
        field
        for field in PLAN_MEMBERSHIP_FIELDS
        if field in update_data and update_data[field] != getattr(task, field)
    ]
    if moved:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "cannot change "
                + ", ".join(moved)
                + " of a task whose Product Brief coverage is not admitted; "
                "finish or abandon the planning attempt first"
            ),
        )
    return task


async def void_superseded_plan(
    *, brief: ProductBrief, story_id: str, superseded_attempt_id: str | None, db: AsyncSession
) -> tuple[list[str], list[str]]:
    """Make the corpse of a superseded plan harmless, in the claim's transaction.

    A takeover mints a new attempt id, and that alone strands everything the
    previous owner planned: the release set of an admission is keyed on the
    *active* attempt, so those tasks are in nobody's release set, the dispatch
    gate refuses them as `product_brief_not_admitted`, and the plan fence
    refuses to move them out of the story while they are unadmitted. Left
    behind, they block the story's completion for ever.

    So the claim voids them. Only the previous attempt's *unadmitted* tasks are
    touched — an admitted task belongs to a plan that already crossed the
    boundary and is ordinary work now. The cancel goes through
    `apply_cancellation`, the same primitive `DELETE /api/tasks/{id}` uses, and
    a row whose status cannot legally reach `cancelled` is left alone and
    reported rather than forced. In practice an unadmitted task can only be
    `backlog` or `todo` — nothing can dispatch it, so nothing can move it past
    those — and both reach `cancelled`.

    The superseded attempt's `RequirementCoverage` rows go with the tasks: they
    are dispositions of a plan that no longer exists, they already do not count
    for any admission (which reads only the active attempt's rows), and half of
    them now point at cancelled tasks. Keeping them would leave the coverage
    table describing work that was voided.

    Caller commits. That is the point: a takeover either voids the old plan and
    opens the new one, or does neither.

    Returns `(cancelled_task_ids, skipped_task_ids)`.
    """
    if superseded_attempt_id is None:
        return [], []
    # Ascending task id, the order the dispatch admission point's rung 1 uses,
    # so a takeover and a dispatch decision queue rather than deadlock. The
    # brief row is already held by the caller, which is this module's lock order.
    tasks = list(
        (
            await db.scalars(
                select(Task)
                .where(
                    Task.story_id == story_id,
                    Task.planning_attempt_id == superseded_attempt_id,
                    Task.dispatch_admitted.is_(False),
                )
                .order_by(Task.id)
                .with_for_update()
            )
        ).all()
    )
    cancelled: list[str] = []
    skipped: list[str] = []
    for task in tasks:
        if not cancellation_is_reachable(task.status):
            skipped.append(task.id)
            continue
        if await apply_cancellation(task, db):
            cancelled.append(task.id)
    await db.execute(
        delete(RequirementCoverage).where(
            RequirementCoverage.brief_id == brief.id,
            RequirementCoverage.planning_attempt_id == superseded_attempt_id,
        )
    )
    return cancelled, skipped
