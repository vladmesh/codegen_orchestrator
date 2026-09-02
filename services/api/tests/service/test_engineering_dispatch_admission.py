"""Service proofs for the one admission point of paid engineering dispatch.

One test per typed refusal reason, each driving the real endpoint into the state
that causes it, plus the admitted path proving a dispatch still produces exactly
the queued Run and metadata it produced when the scheduler decided these
conditions itself.
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
import uuid

from httpx import AsyncClient
import pytest
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.engineering_dispatch import (
    EngineeringDispatchOutcome,
    EngineeringDispatchRefusal,
    EngineeringDispatchRepair,
)
from shared.contracts.dto.executor_diagnostics import (
    EXECUTOR_DIAGNOSTICS_REDIS_KEY,
    ExecutorAuthMode,
    ExecutorAvailability,
    ExecutorDiagnostic,
    ExecutorDiagnosticSnapshot,
    safe_executor_diagnostic_reason,
)
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.vocab import AgentType
from shared.models import (
    EngineeringBudgetReservation,
    Project,
    Run,
    Story,
    SystemConfig,
    Task,
    TaskEvent,
    User,
)
from src.engineering_dispatch_admission import INTERNAL_PROJECT_ID

ADMISSION_URL = "/api/work-admission/engineering-dispatches"


async def _decide(client: AsyncClient, task_id: str) -> dict:
    response = await client.post(ADMISSION_URL, json={"task_id": task_id})
    assert response.status_code == HTTPStatus.OK, response.text
    return response.json()


async def _owner(client: AsyncClient) -> int:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    created = await client.post(
        "/api/users/", json={"telegram_id": telegram_id, "username": f"dispatch-{telegram_id}"}
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    return telegram_id


async def _project(
    client: AsyncClient,
    telegram_id: int,
    *,
    status: str = "active",
    config: dict | None = None,
) -> str:
    project_id = str(uuid.uuid4())
    created = await client.post(
        "/api/projects/",
        headers={"X-Telegram-ID": str(telegram_id)},
        json={
            "id": project_id,
            "title": "Dispatch admission",
            "initiating_run_id": f"init-{uuid.uuid4().hex}",
            "status": status,
            "config": config if config is not None else {"workspace_ready": True},
        },
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    return project_id


async def _task(client: AsyncClient, project_id: str, **fields) -> str:
    body = {
        "project_id": project_id,
        "type": "feature",
        "title": "Dispatch me",
        "status": "todo",
        **fields,
    }
    created = await client.post("/api/tasks/", json=body)
    assert created.status_code == HTTPStatus.CREATED, created.text
    return created.json()["id"]


async def _story(client: AsyncClient, project_id: str) -> str:
    created = await client.post(
        "/api/stories/", json={"project_id": project_id, "title": "One branch"}
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    return created.json()["id"]


async def _set_status(db_session: AsyncSession, task_id: str, status: str) -> None:
    """Put a task in a status directly — the transition chain is not under test."""
    task = await db_session.get(Task, task_id)
    task.status = status
    await db_session.commit()


async def _blocked_pair(
    client: AsyncClient, project_id: str, *, blocker_sorts_first: bool
) -> tuple[str, str]:
    """A (dependent, blocker) pair whose ids sort in the requested direction.

    Task ids are random, so the pair is drawn until it lands the way the test
    needs. Both directions are exercised, which is the only way to tell an
    ascending-id lock order from one that happens to follow the dependency edge.
    """
    for _ in range(40):
        blocker = await _task(client, project_id, title="Blocker")
        dependent = await _task(client, project_id, title="Dependent", blocked_by_task_id=blocker)
        if (blocker < dependent) is blocker_sorts_first:
            return dependent, blocker
    pytest.fail("could not draw a task id pair in the requested order")


@pytest.fixture
async def paid_controls(db_session: AsyncSession) -> AsyncGenerator[dict, None]:
    """Restore every paid control this module writes, whatever the test does."""
    keys = [
        "work_admission.emergency_stop",
        "work_admission.max_concurrent_paid_runs",
    ]
    rows = {key: await db_session.get(SystemConfig, key) for key in keys}
    before = {key: row.value for key, row in rows.items()}
    yield rows
    for key, row in rows.items():
        row.value = before[key]
    await db_session.commit()


# --- refusals decided before anything is counted ----------------------------


@pytest.mark.asyncio
async def test_a_task_that_left_todo_is_not_dispatchable(
    async_client: AsyncClient, db_session: AsyncSession
):
    """The lock is taken to be used: the row's own status is read under it."""
    project_id = await _project(async_client, await _owner(async_client))
    task_id = await _task(async_client, project_id)
    await _set_status(db_session, task_id, "in_dev")

    decision = await _decide(async_client, task_id)

    assert decision["outcome"] == EngineeringDispatchOutcome.REFUSED
    assert decision["reason"] == EngineeringDispatchRefusal.TASK_NOT_DISPATCHABLE


@pytest.mark.asyncio
async def test_the_internal_project_is_never_dispatched(
    async_client: AsyncClient, db_session: AsyncSession
):
    """The orchestrator's own tasks are implemented by hand, and this says so.

    The condition used to be a hardcoded UUID compared inside the scheduler loop.
    It is the same UUID, now one named condition of the admission point with a
    reason an operator can read.
    """
    owner = await _owner(async_client)
    internal = await db_session.get(Project, INTERNAL_PROJECT_ID)
    if internal is None:
        user = (
            await db_session.execute(select(User).where(User.telegram_id == owner))
        ).scalar_one()
        db_session.add(
            Project(
                id=INTERNAL_PROJECT_ID,
                title="Internal",
                slug=f"internal-{uuid.uuid4().hex[:8]}",
                status="active",
                config={"workspace_ready": True},
                owner_id=user.id,
                initiating_run_id="init-internal",
            )
        )
        await db_session.commit()
    task_id = await _task(async_client, str(INTERNAL_PROJECT_ID))

    decision = await _decide(async_client, task_id)

    assert decision["reason"] == EngineeringDispatchRefusal.INTERNAL_PROJECT
    assert decision["run_id"] is None


@pytest.mark.asyncio
async def test_an_unresolved_blocker_refuses_and_a_done_one_does_not(
    async_client: AsyncClient, db_session: AsyncSession
):
    """`blocked_by_task_id` is read through the blocker's real status."""
    project_id = await _project(async_client, await _owner(async_client))
    blocker_id = await _task(async_client, project_id, title="Blocker")
    blocked_id = await _task(async_client, project_id, blocked_by_task_id=blocker_id)

    refused = await _decide(async_client, blocked_id)
    assert refused["reason"] == EngineeringDispatchRefusal.BLOCKER_UNRESOLVED

    await _set_status(db_session, blocker_id, "done")
    admitted = await _decide(async_client, blocked_id)
    assert admitted["outcome"] == EngineeringDispatchOutcome.ADMITTED


@pytest.mark.asyncio
async def test_a_project_that_predates_run_ownership_cannot_dispatch(
    async_client: AsyncClient, db_session: AsyncSession
):
    """No run to attribute a worker to, and none can be reconstructed."""
    telegram_id = await _owner(async_client)
    user = (
        await db_session.execute(select(User).where(User.telegram_id == telegram_id))
    ).scalar_one()
    suffix = uuid.uuid4().hex[:8]
    project = Project(
        id=uuid.uuid4(),
        title=f"Legacy {suffix}",
        slug=f"legacy-dispatch-{suffix}",
        status="active",
        config={"workspace_ready": True},
        owner_id=user.id,
        initiating_run_id=None,
    )
    db_session.add(project)
    await db_session.commit()
    task_id = await _task(async_client, str(project.id))

    decision = await _decide(async_client, task_id)

    assert decision["reason"] == EngineeringDispatchRefusal.PROJECT_HAS_NO_INITIATING_RUN

    await db_session.execute(
        delete(TaskEvent).where(
            TaskEvent.task_id.in_(select(Task.id).where(Task.project_id == project.id))
        )
    )
    await db_session.execute(delete(Task).where(Task.project_id == project.id))
    await db_session.execute(delete(Project).where(Project.id == project.id))
    await db_session.commit()


@pytest.mark.asyncio
async def test_a_draft_project_has_not_been_scaffolded_yet(async_client: AsyncClient):
    """There is no repository for a worker to check out."""
    project_id = await _project(async_client, await _owner(async_client), status="draft")
    task_id = await _task(async_client, project_id)

    decision = await _decide(async_client, task_id)

    assert decision["reason"] == EngineeringDispatchRefusal.PROJECT_NOT_SCAFFOLDED


@pytest.mark.asyncio
async def test_a_project_without_a_ready_workspace_refuses(async_client: AsyncClient):
    """Scaffolded is not the same as ready to be written in."""
    project_id = await _project(async_client, await _owner(async_client), config={})
    task_id = await _task(async_client, project_id)

    decision = await _decide(async_client, task_id)

    assert decision["reason"] == EngineeringDispatchRefusal.WORKSPACE_NOT_READY


@pytest.mark.asyncio
async def test_a_story_with_a_task_in_dev_is_busy(
    async_client: AsyncClient, db_session: AsyncSession
):
    """One task in flight per story: a branch is written by one worker at a time."""
    project_id = await _project(async_client, await _owner(async_client))
    story_id = await _story(async_client, project_id)
    sibling_id = await _task(async_client, project_id, story_id=story_id, title="Sibling")
    task_id = await _task(async_client, project_id, story_id=story_id)
    await _set_status(db_session, sibling_id, "in_dev")

    decision = await _decide(async_client, task_id)

    assert decision["reason"] == EngineeringDispatchRefusal.STORY_BUSY


@pytest.mark.asyncio
async def test_a_story_with_a_sibling_in_human_review_takes_no_new_work(
    async_client: AsyncClient, db_session: AsyncSession
):
    """A story that gave up on a human takes none of its tasks further."""
    project_id = await _project(async_client, await _owner(async_client))
    story_id = await _story(async_client, project_id)
    sibling_id = await _task(async_client, project_id, story_id=story_id, title="Gave up")
    task_id = await _task(async_client, project_id, story_id=story_id)
    await _set_status(db_session, sibling_id, "waiting_human_review")

    decision = await _decide(async_client, task_id)

    assert decision["reason"] == EngineeringDispatchRefusal.STORY_WAITING_HUMAN_REVIEW


@pytest.mark.asyncio
async def test_a_live_attempt_is_adopted_and_never_dispatched_over(
    async_client: AsyncClient, db_session: AsyncSession
):
    """The fence: a live run of an earlier iteration still owns the branch.

    `current_iteration` is bumped by the very retry that creates the risk, so it
    decides nothing here. The decision names the repair and creates no attempt.
    """
    project_id = await _project(async_client, await _owner(async_client))
    task_id = await _task(async_client, project_id)
    task = await db_session.get(Task, task_id)
    task.current_iteration = 1
    db_session.add(
        Run(
            id=f"eng-live-{uuid.uuid4().hex[:8]}",
            type=RunType.ENGINEERING.value,
            status=RunStatus.RUNNING.value,
            project_id=uuid.UUID(project_id),
            task_id=task_id,
            run_metadata={"triggered_by": "dispatcher", "iteration": 0},
        )
    )
    await db_session.commit()

    decision = await _decide(async_client, task_id)

    assert decision["outcome"] == EngineeringDispatchOutcome.REPAIR
    assert decision["repair"] == EngineeringDispatchRepair.ADOPT_LIVE_ATTEMPT
    assert decision["reason"] == EngineeringDispatchRefusal.LIVE_ATTEMPT_IN_FLIGHT
    assert decision["paid_work"] is None
    runs = (await db_session.scalars(select(Run).where(Run.task_id == task_id))).all()
    assert len(runs) == 1


@pytest.mark.asyncio
async def test_a_finished_run_of_this_iteration_is_replayed_not_redispatched(
    async_client: AsyncClient, db_session: AsyncSession
):
    """A worker that finished while the task was stuck in todo owes it its outcome."""
    project_id = await _project(async_client, await _owner(async_client))
    task_id = await _task(async_client, project_id)
    run_id = f"eng-finished-{uuid.uuid4().hex[:8]}"
    db_session.add(
        Run(
            id=run_id,
            type=RunType.ENGINEERING.value,
            status=RunStatus.COMPLETED.value,
            project_id=uuid.UUID(project_id),
            task_id=task_id,
            run_metadata={"triggered_by": "dispatcher", "iteration": 0},
            result={"engineering_status": "done"},
        )
    )
    await db_session.commit()

    decision = await _decide(async_client, task_id)

    assert decision["repair"] == EngineeringDispatchRepair.REPLAY_FINISHED_RUN
    assert decision["run_id"] == run_id


@pytest.mark.asyncio
async def test_a_pre_handoff_abort_is_not_an_attempt_and_dispatches_again(
    async_client: AsyncClient, db_session: AsyncSession
):
    """A run aborted before queue handoff proved no message reached a worker."""
    project_id = await _project(async_client, await _owner(async_client))
    task_id = await _task(async_client, project_id)
    db_session.add(
        Run(
            id=f"eng-aborted-{uuid.uuid4().hex[:8]}",
            type=RunType.ENGINEERING.value,
            status=RunStatus.CANCELLED.value,
            project_id=uuid.UUID(project_id),
            task_id=task_id,
            run_metadata={"iteration": 0, "pre_handoff_aborted": True},
        )
    )
    await db_session.commit()

    decision = await _decide(async_client, task_id)

    assert decision["outcome"] == EngineeringDispatchOutcome.ADMITTED


# --- refusals from the paid gate this module wraps --------------------------


@pytest.mark.asyncio
async def test_the_emergency_stop_refuses_engineering_dispatch(
    async_client: AsyncClient, db_session: AsyncSession, paid_controls: dict
):
    """The operator's stop reaches dispatch through the gate it always did."""
    project_id = await _project(async_client, await _owner(async_client))
    task_id = await _task(async_client, project_id)
    paid_controls["work_admission.emergency_stop"].value = True
    await db_session.commit()

    decision = await _decide(async_client, task_id)

    assert decision["reason"] == EngineeringDispatchRefusal.EMERGENCY_STOP
    assert decision["paid_work"]["admission"]["reason"] == "emergency_stop"


@pytest.mark.asyncio
async def test_a_full_paid_slot_table_defers_the_dispatch(
    async_client: AsyncClient, db_session: AsyncSession, paid_controls: dict
):
    """The concurrency ceiling is counted server-side, as it always was."""
    project_id = await _project(async_client, await _owner(async_client))
    task_id = await _task(async_client, project_id)
    paid_controls["work_admission.max_concurrent_paid_runs"].value = 0
    await db_session.commit()

    decision = await _decide(async_client, task_id)

    assert decision["reason"] == EngineeringDispatchRefusal.PAID_WORK_LIMIT
    assert decision["paid_work"]["admission"]["retryable"] is True


@pytest.mark.asyncio
async def test_an_exhausted_engineering_budget_denies_the_dispatch(
    async_client: AsyncClient, db_session: AsyncSession
):
    """The budget decision the dispatcher routes to human review comes from here."""
    telegram_id = await _owner(async_client)
    user = (
        await db_session.execute(select(User).where(User.telegram_id == telegram_id))
    ).scalar_one()
    policy = await async_client.put(
        f"/api/engineering-budget-policies/{user.id}",
        json={
            "limit_microusd": 1_000,
            "attempt_reservation_microusd": 10_000,
            "state": "enabled",
        },
    )
    assert policy.status_code == HTTPStatus.CREATED, policy.text
    project_id = await _project(async_client, telegram_id)
    task_id = await _task(async_client, project_id)

    decision = await _decide(async_client, task_id)

    assert decision["reason"] == EngineeringDispatchRefusal.ENGINEERING_BUDGET_DENIED
    assert decision["paid_work"]["engineering_budget"]["outcome"] == "denied"
    # Nothing queued: the refusal is the whole effect.
    assert (await db_session.scalars(select(Run).where(Run.task_id == task_id))).all() == []


async def _publish_diagnostics(redis_client: Redis, reason_code: str, *, version: str) -> None:
    """Publish the snapshot both executors are in the named safe state.

    A diagnostic carries the whole state its reason code is allowed to describe,
    so availability and the lease count come from the code rather than being
    chosen next to it.
    """
    availability, lease_count = {
        "local_auth_invalid": (ExecutorAvailability.UNAVAILABLE, 0),
        "inventory_unreconciled": (ExecutorAvailability.UNKNOWN, None),
    }[reason_code]
    now = datetime.now(UTC)
    expiry = now + timedelta(minutes=5)
    await redis_client.set(
        EXECUTOR_DIAGNOSTICS_REDIS_KEY,
        ExecutorDiagnosticSnapshot(
            schema_version="v1",
            version=version,
            observed_at=now,
            expires_at=expiry,
            diagnostics=[
                ExecutorDiagnostic(
                    executor=executor,
                    enabled=True,
                    auth_mode=ExecutorAuthMode.HOST_SESSION,
                    availability=availability,
                    observed_at=now,
                    expires_at=expiry,
                    active_lease_count=lease_count,
                    reason_code=reason_code,
                    reason=safe_executor_diagnostic_reason(reason_code),
                )
                for executor in (AgentType.CLAUDE, AgentType.CODEX)
            ],
        ).model_dump_json(),
        ex=300,
    )


@pytest.mark.asyncio
async def test_an_unavailable_executor_denies_the_dispatch(
    async_client: AsyncClient, db_session: AsyncSession, redis_client: Redis
):
    """No executor to run the attempt, so no attempt is created or held."""
    project_id = await _project(async_client, await _owner(async_client))
    task_id = await _task(async_client, project_id)
    await _publish_diagnostics(
        redis_client, "local_auth_invalid", version=f"down-{uuid.uuid4().hex[:8]}"
    )

    decision = await _decide(async_client, task_id)

    assert decision["reason"] == EngineeringDispatchRefusal.EXECUTOR_UNAVAILABLE
    assert (await db_session.scalars(select(Run).where(Run.task_id == task_id))).all() == []


@pytest.mark.asyncio
async def test_an_unknown_executor_state_needs_an_administrator(
    async_client: AsyncClient, redis_client: Redis
):
    """An unconfirmed unknown state is a deferral, not a denial."""
    project_id = await _project(async_client, await _owner(async_client))
    task_id = await _task(async_client, project_id)
    await _publish_diagnostics(
        redis_client, "inventory_unreconciled", version=f"unknown-{uuid.uuid4().hex[:8]}"
    )

    decision = await _decide(async_client, task_id)

    assert decision["reason"] == EngineeringDispatchRefusal.EXECUTOR_CONFIRMATION_REQUIRED
    assert decision["paid_work"]["admission"]["outcome"] == "deferred"


# --- the admitted path ------------------------------------------------------


@pytest.mark.asyncio
async def test_an_admitted_task_dispatches_exactly_as_before(
    async_client: AsyncClient, db_session: AsyncSession
):
    """The attempt the scheduler used to create itself, created here instead.

    Same queued engineering Run, same metadata the message and the next tick's
    fence read off it — story, task, initiating run, iteration — and the budget
    hold taken in the same transaction.
    """
    telegram_id = await _owner(async_client)
    project_id = await _project(async_client, telegram_id)
    project = await async_client.get(f"/api/projects/{project_id}")
    initiating_run_id = project.json()["initiating_run_id"]
    story_id = await _story(async_client, project_id)
    task_id = await _task(async_client, project_id, story_id=story_id)
    task = await db_session.get(Task, task_id)
    task.current_iteration = 2
    await db_session.commit()

    decision = await _decide(async_client, task_id)

    assert decision["outcome"] == EngineeringDispatchOutcome.ADMITTED
    assert decision["reason"] is None
    assert decision["initiating_run_id"] == initiating_run_id
    run = await db_session.get(Run, decision["run_id"])
    assert run is not None
    assert run.status == RunStatus.QUEUED.value
    assert run.type == RunType.ENGINEERING.value
    assert run.task_id == task_id
    assert run.story_id == story_id
    assert run.run_metadata["triggered_by"] == "dispatcher"
    assert run.run_metadata["story_id"] == story_id
    assert run.run_metadata["task_id"] == task_id
    assert run.run_metadata["initiating_run_id"] == initiating_run_id
    assert run.run_metadata["iteration"] == 2
    reservation = await db_session.scalar(
        select(EngineeringBudgetReservation).where(
            EngineeringBudgetReservation.attempt_id == decision["run_id"]
        )
    )
    assert reservation is not None
    # The task itself is untouched: leaving todo is the caller's step, after the
    # message is out.
    assert (await db_session.get(Task, task_id)).status == "todo"


@pytest.mark.asyncio
async def test_a_story_whose_siblings_are_done_still_dispatches(
    async_client: AsyncClient, db_session: AsyncSession
):
    """Only in_dev and human review hold a story; a finished sibling does not."""
    project_id = await _project(async_client, await _owner(async_client))
    story_id = await _story(async_client, project_id)
    done_id = await _task(async_client, project_id, story_id=story_id, title="Finished")
    failed_id = await _task(async_client, project_id, story_id=story_id, title="Failed")
    task_id = await _task(async_client, project_id, story_id=story_id)
    await _set_status(db_session, done_id, "done")
    await _set_status(db_session, failed_id, "failed")

    decision = await _decide(async_client, task_id)

    assert decision["outcome"] == EngineeringDispatchOutcome.ADMITTED
    assert (await db_session.get(Story, story_id)) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker_sorts_first", [True, False])
async def test_both_task_rows_are_taken_in_ascending_id_order(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, blocker_sorts_first: bool
):
    """The blocker is read for update, and the order does not follow the edge.

    A row read in order to decide action is locked, so a concurrent legal
    `done -> backlog` on the blocker cannot be lost between the read and the
    commit. Ascending task id — not dependency order — is what keeps a reciprocal
    dependency from deadlocking: both dispatches take the same two rows in the
    same order, so one waits.
    """
    from src.routers import _task_helpers

    project_id = await _project(async_client, await _owner(async_client))
    dependent, blocker = await _blocked_pair(
        async_client, project_id, blocker_sorts_first=blocker_sorts_first
    )
    locked: list[str] = []
    original = _task_helpers.get_task_for_update

    async def recording(task_id: str, db: AsyncSession):
        locked.append(task_id)
        return await original(task_id, db)

    monkeypatch.setattr(_task_helpers, "get_task_for_update", recording)
    locked.clear()

    decision = await _decide(async_client, dependent)

    assert decision["reason"] == EngineeringDispatchRefusal.BLOCKER_UNRESOLVED
    assert locked == sorted([dependent, blocker])


@pytest.mark.asyncio
async def test_a_sibling_holding_a_live_run_makes_the_story_busy(
    async_client: AsyncClient, db_session: AsyncSession
):
    """The fence observes runs, not only statuses.

    A sibling that was admitted and published but whose transition out of todo
    failed is a supported recovery state: still todo, with a live run whose worker
    holds the story branch. A status-only scan cannot see it, and a second worker
    would go onto the same branch.
    """
    project_id = await _project(async_client, await _owner(async_client))
    story_id = await _story(async_client, project_id)
    sibling_id = await _task(async_client, project_id, story_id=story_id, title="Published")
    task_id = await _task(async_client, project_id, story_id=story_id)
    db_session.add(
        Run(
            id=f"eng-sibling-{uuid.uuid4().hex[:8]}",
            type=RunType.ENGINEERING.value,
            status=RunStatus.QUEUED.value,
            project_id=uuid.UUID(project_id),
            task_id=sibling_id,
            story_id=story_id,
            run_metadata={"triggered_by": "dispatcher", "iteration": 0},
        )
    )
    await db_session.commit()
    assert (await db_session.get(Task, sibling_id)).status == "todo"

    decision = await _decide(async_client, task_id)

    assert decision["reason"] == EngineeringDispatchRefusal.STORY_BUSY
    assert (await db_session.scalars(select(Run).where(Run.task_id == task_id))).all() == []


@pytest.mark.asyncio
async def test_a_siblings_finished_or_aborted_run_does_not_hold_the_story(
    async_client: AsyncClient, db_session: AsyncSession
):
    """Only a live run holds the branch, and an aborted one never reached a worker."""
    project_id = await _project(async_client, await _owner(async_client))
    story_id = await _story(async_client, project_id)
    sibling_id = await _task(async_client, project_id, story_id=story_id, title="Finished")
    task_id = await _task(async_client, project_id, story_id=story_id)
    db_session.add_all(
        [
            Run(
                id=f"eng-done-{uuid.uuid4().hex[:8]}",
                type=RunType.ENGINEERING.value,
                status=RunStatus.COMPLETED.value,
                project_id=uuid.UUID(project_id),
                task_id=sibling_id,
                story_id=story_id,
                run_metadata={"iteration": 0},
            ),
            Run(
                id=f"eng-aborted-{uuid.uuid4().hex[:8]}",
                type=RunType.ENGINEERING.value,
                status=RunStatus.QUEUED.value,
                project_id=uuid.UUID(project_id),
                task_id=sibling_id,
                story_id=story_id,
                run_metadata={"iteration": 1, "pre_handoff_aborted": True},
            ),
        ]
    )
    await db_session.commit()

    decision = await _decide(async_client, task_id)

    assert decision["outcome"] == EngineeringDispatchOutcome.ADMITTED


# --- overrides: named, evaluated, recorded ----------------------------------


@pytest.mark.asyncio
async def test_a_named_override_walks_past_its_condition_and_is_recorded(
    async_client: AsyncClient, db_session: AsyncSession
):
    """An override is not an absence of admission: the condition still ran.

    The decision says which refusal it walked past and the attempt it created
    records the same thing, so a dispatch that only happened because somebody was
    authorised to say so can be read back off the run.
    """
    project_id = await _project(async_client, await _owner(async_client))
    task_id = await _task(async_client, project_id)
    await _set_status(db_session, task_id, "in_dev")

    response = await async_client.post(
        ADMISSION_URL,
        json={
            "task_id": task_id,
            "origin": "admin",
            "overrides": [EngineeringDispatchRefusal.TASK_NOT_DISPATCHABLE.value],
        },
    )
    assert response.status_code == HTTPStatus.OK, response.text
    decision = response.json()

    assert decision["outcome"] == EngineeringDispatchOutcome.ADMITTED
    assert decision["overridden"] == [EngineeringDispatchRefusal.TASK_NOT_DISPATCHABLE]
    run = await db_session.get(Run, decision["run_id"])
    assert run.run_metadata["triggered_by"] == "admin"
    assert run.run_metadata["admission_overrides"] == [
        EngineeringDispatchRefusal.TASK_NOT_DISPATCHABLE.value
    ]


@pytest.mark.asyncio
async def test_an_override_clears_only_the_condition_it_names(
    async_client: AsyncClient, db_session: AsyncSession
):
    """Every other condition refuses an overriding caller exactly as before."""
    project_id = await _project(async_client, await _owner(async_client))
    story_id = await _story(async_client, project_id)
    sibling_id = await _task(async_client, project_id, story_id=story_id, title="Working")
    task_id = await _task(async_client, project_id, story_id=story_id)
    await _set_status(db_session, sibling_id, "in_dev")
    await _set_status(db_session, task_id, "backlog")

    response = await async_client.post(
        ADMISSION_URL,
        json={
            "task_id": task_id,
            "overrides": [EngineeringDispatchRefusal.TASK_NOT_DISPATCHABLE.value],
        },
    )
    assert response.status_code == HTTPStatus.OK, response.text
    decision = response.json()

    assert decision["reason"] == EngineeringDispatchRefusal.STORY_BUSY
    assert decision["overridden"] == [EngineeringDispatchRefusal.TASK_NOT_DISPATCHABLE]
    assert (await db_session.scalars(select(Run).where(Run.task_id == task_id))).all() == []


@pytest.mark.asyncio
async def test_the_paid_gate_and_the_project_conditions_cannot_be_overridden(
    async_client: AsyncClient,
):
    """The command itself refuses to carry an override nobody may have."""
    project_id = await _project(async_client, await _owner(async_client))
    task_id = await _task(async_client, project_id)

    for reason in (
        EngineeringDispatchRefusal.ENGINEERING_BUDGET_DENIED,
        EngineeringDispatchRefusal.WORKSPACE_NOT_READY,
        EngineeringDispatchRefusal.PROJECT_NOT_SCAFFOLDED,
        EngineeringDispatchRefusal.EMERGENCY_STOP,
    ):
        response = await async_client.post(
            ADMISSION_URL, json={"task_id": task_id, "overrides": [reason.value]}
        )
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text


@pytest.mark.asyncio
async def test_a_task_is_not_its_own_busy_sibling(
    async_client: AsyncClient, db_session: AsyncSession
):
    """The story condition asks what *else* holds the branch.

    An operator respawning a worker for a task already in_dev overrides the
    dispatchability status; it must not then be refused by its own row turning up
    in the story's sibling scan.
    """
    project_id = await _project(async_client, await _owner(async_client))
    story_id = await _story(async_client, project_id)
    task_id = await _task(async_client, project_id, story_id=story_id)
    await _set_status(db_session, task_id, "in_dev")

    response = await async_client.post(
        ADMISSION_URL,
        json={
            "task_id": task_id,
            "origin": "admin",
            "overrides": [EngineeringDispatchRefusal.TASK_NOT_DISPATCHABLE.value],
        },
    )
    assert response.status_code == HTTPStatus.OK, response.text

    assert response.json()["outcome"] == EngineeringDispatchOutcome.ADMITTED
