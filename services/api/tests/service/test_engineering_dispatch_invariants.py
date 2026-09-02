"""The two invariants of the declared admission point, exhibited against the database.

**Invariant A — one lock ladder, and every condition row on it.** The admission
transaction decides only on rows it holds. `test_every_condition_row_is_held_...`
proves that by probing each of them from a second, genuinely concurrent
transaction while the decision is open: every probe has to time out on the lock.
The probe goes red for whichever row is read unlocked, so it is one test per row
rather than one assertion of intent.

**The corollary — no caller may materialise a subject row before calling
admission.** SQLAlchemy hands a later `SELECT ... FOR UPDATE` the entity a plain
read already put in the session, stale attributes and all. Two tests here:
`spawn-worker` decides on the locked row against a real concurrent write, and the
counterfactual next to it shows the same admission call deciding on the stale row
when a caller materialises it first. That counterfactual is the reason the route
peeks column-only.

**Invariant B — one entry to paid engineering work on a Task.**
`POST /work-admission/paid-runs` refuses an engineering command whose `task_id`
names a Task row, and still starts every paid engineering run that is not a Task
dispatch — the deploy-fix handoff among them, which carries no Task row.

Every test drives the real endpoints against the real database; the monkeypatched
wrappers below are scheduling points, not stubs — the write they interleave is a
real transaction on another connection, and the call they wrap is the original.
"""

from collections.abc import Callable
from http import HTTPStatus
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shared.contracts.dto.engineering_dispatch import (
    ENGINEERING_TASK_REQUIRES_ADMISSION,
    EngineeringDispatchOutcome,
    EngineeringDispatchRefusal,
)
from shared.contracts.dto.run import RunStatus, RunType
from shared.models import Project, Run, Story, Task

PAID_RUNS_URL = "/api/work-admission/paid-runs"
ADMISSION_URL = "/api/work-admission/engineering-dispatches"


async def _owner(client: AsyncClient) -> int:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    created = await client.post(
        "/api/users/", json={"telegram_id": telegram_id, "username": f"ladder-{telegram_id}"}
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    return telegram_id


async def _project(client: AsyncClient, telegram_id: int) -> str:
    project_id = str(uuid.uuid4())
    created = await client.post(
        "/api/projects/",
        headers={"X-Telegram-ID": str(telegram_id)},
        json={
            "id": project_id,
            "title": "Ladder",
            "initiating_run_id": f"init-{uuid.uuid4().hex}",
            "status": "active",
            "config": {"workspace_ready": True},
        },
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    return project_id


async def _task(client: AsyncClient, project_id: str, **fields) -> str:
    created = await client.post(
        "/api/tasks/",
        json={
            "project_id": project_id,
            "type": "feature",
            "title": "Ladder task",
            "status": "todo",
            **fields,
        },
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    return created.json()["id"]


async def _story(client: AsyncClient, project_id: str) -> str:
    created = await client.post(
        "/api/stories/", json={"project_id": project_id, "title": "One branch"}
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    return created.json()["id"]


def _other_session(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A second session factory — a real second connection, not a nested one."""
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


async def _row_is_held(db_engine: AsyncEngine, statement) -> bool:
    """Does another transaction block on this row right now?

    A short `lock_timeout` turns "held by somebody else" into an error the test
    can assert on, so the probe cannot hang the suite when the answer is yes and
    cannot pass by accident when the answer is no.
    """
    async with _other_session(db_engine)() as probe:
        await probe.execute(text("SET LOCAL lock_timeout = '400ms'"))
        try:
            await probe.execute(statement)
        except DBAPIError:
            await probe.rollback()
            return True
        await probe.rollback()
        return False


# --- Invariant B: one entry to paid engineering work on a Task --------------


@pytest.mark.asyncio
async def test_a_paid_engineering_start_naming_a_task_row_is_refused(
    async_client: AsyncClient, db_session: AsyncSession
):
    """The lower paid-run endpoint is not a second admission surface.

    An authorised internal or admin caller could post an engineering start naming
    a real Task id and get a queued billable attempt with none of the admission
    conditions run. That command *is* a Task dispatch, so it is refused here with
    a typed code and has to go through the admission point.
    """
    project_id = await _project(async_client, await _owner(async_client))
    task_id = await _task(async_client, project_id)

    response = await async_client.post(
        PAID_RUNS_URL,
        json={
            "id": f"eng-{uuid.uuid4().hex[:12]}",
            "type": RunType.ENGINEERING.value,
            "project_id": project_id,
            "task_id": task_id,
        },
    )

    assert response.status_code == HTTPStatus.CONFLICT, response.text
    assert response.json()["detail"]["code"] == ENGINEERING_TASK_REQUIRES_ADMISSION
    assert response.json()["detail"]["task_id"] == task_id
    # Nothing was counted: the refusal precedes every paid control.
    assert (await db_session.scalars(select(Run).where(Run.task_id == task_id))).all() == []


@pytest.mark.asyncio
async def test_a_paid_engineering_start_that_is_no_task_dispatch_still_starts(
    async_client: AsyncClient, db_session: AsyncSession
):
    """Everything Invariant B does not name is decided by the paid gate as before.

    The refusal asks one question — does this `task_id` name a Task row — so a
    paid engineering start that is not a dispatch of a Task passes through
    untouched. The deploy-fix handoff is that shape: no Task row backs it, and
    `runs.task_id` is a foreign key onto `tasks.id`, so the only engineering run
    the database can persist with a `task_id` at all is one naming a real Task.
    That is what makes the existence check a complete fence rather than a partial
    one.

    The deploy supervisor still puts its synthesised id in `task_id`
    (`supervisor/deploy.py`), which that foreign key rejects on main as it does
    here: a defect this branch neither introduces nor repairs, and named in the
    card report rather than fixed under an admission card.
    """
    project_id = await _project(async_client, await _owner(async_client))
    fix_id = f"eng-deploy-fix-{uuid.uuid4().hex[:8]}-1"

    response = await async_client.post(
        PAID_RUNS_URL,
        json={
            "id": fix_id,
            "type": RunType.ENGINEERING.value,
            "project_id": project_id,
            "task_id": None,
            "run_metadata": {"deploy_fix_attempt": 1},
        },
    )

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.json()["admission"]["outcome"] == "admitted"
    run = await db_session.get(Run, fix_id)
    assert run is not None
    assert run.status == RunStatus.QUEUED.value


@pytest.mark.asyncio
async def test_a_qa_start_naming_a_task_row_is_untouched(
    async_client: AsyncClient, db_session: AsyncSession
):
    """Invariant B is about engineering dispatch, and refuses nothing else."""
    project_id = await _project(async_client, await _owner(async_client))
    task_id = await _task(async_client, project_id)
    qa_id = f"qa-{uuid.uuid4().hex[:12]}"

    response = await async_client.post(
        PAID_RUNS_URL,
        json={
            "id": qa_id,
            "type": RunType.QA.value,
            "project_id": project_id,
            "task_id": task_id,
        },
    )

    assert response.status_code == HTTPStatus.OK, response.text
    assert (await db_session.get(Run, qa_id)) is not None


# --- the corollary: no caller materialises a subject row before admission ---


@pytest.mark.asyncio
async def test_spawn_worker_decides_on_the_locked_row_not_a_pre_read_one(
    async_client: AsyncClient,
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
):
    """A concurrent write between the route's peek and the lock is not lost.

    The route peeks the candidate's status column-only, because it may only start
    a worker from a status it can move. A concurrent transaction then gives the
    task an unresolved blocker and commits, before admission takes its row locks.
    The condition must be decided on the locked row — the one that now names a
    blocker — and not on anything the route read first.
    """
    from src.routers import _task_helpers

    project_id = await _project(async_client, await _owner(async_client))
    blocker_id = await _task(async_client, project_id, title="Blocker")
    task_id = await _task(async_client, project_id, status="backlog")

    original = _task_helpers.get_task_for_update
    interleaved: list[str] = []

    async def transition_then_lock(wanted_id: str, db: AsyncSession) -> Task:
        if not interleaved:
            interleaved.append(wanted_id)
            async with _other_session(db_engine)() as concurrent:
                await concurrent.execute(
                    update(Task).where(Task.id == task_id).values(blocked_by_task_id=blocker_id)
                )
                await concurrent.commit()
        return await original(wanted_id, db)

    monkeypatch.setattr(_task_helpers, "get_task_for_update", transition_then_lock)

    response = await async_client.post(f"/api/tasks/{task_id}/spawn-worker", json={"actor": "op"})

    assert interleaved, "the concurrent write never ran"
    assert response.status_code == HTTPStatus.CONFLICT, response.text
    assert EngineeringDispatchRefusal.BLOCKER_UNRESOLVED.value.replace("_", " ") in str(
        response.json()["detail"]
    )
    # No worker was bought and the task did not leave the status it was in.
    assert (await db_session.scalars(select(Run).where(Run.task_id == task_id))).all() == []
    assert await db_session.scalar(select(Task.status).where(Task.id == task_id)) == "backlog"


@pytest.mark.asyncio
async def test_a_caller_that_materialises_the_task_first_decides_on_a_stale_row(
    async_client: AsyncClient,
    db_session: AsyncSession,
    db_engine: AsyncEngine,
):
    """Why the corollary exists, executed rather than asserted.

    This is the counterfactual for the test above: the same admission call, the
    same concurrent write, the one difference being that the caller loaded the
    Task as an entity first. SQLAlchemy's identity map then hands the locking read
    that already-materialised object without refreshing its attributes, so the
    blocker condition sees the value the plain read saw and admits. The row lock
    is taken either way — it is the pre-read that loses the write, which is why
    no route may hold a subject entity before calling admission.
    """
    from shared.contracts.dto.engineering_dispatch import EngineeringDispatchCommand
    from src.engineering_dispatch_admission import admit_engineering_dispatch

    project_id = await _project(async_client, await _owner(async_client))
    blocker_id = await _task(async_client, project_id, title="Blocker")
    task_id = await _task(async_client, project_id)

    async with _other_session(db_engine)() as caller:
        # The forbidden move: an entity in the session before admission runs.
        pre_read = await caller.get(Task, task_id)
        assert pre_read.blocked_by_task_id is None

        async with _other_session(db_engine)() as concurrent:
            await concurrent.execute(
                update(Task).where(Task.id == task_id).values(blocked_by_task_id=blocker_id)
            )
            await concurrent.commit()

        decision = await admit_engineering_dispatch(
            EngineeringDispatchCommand(task_id=task_id), caller
        )
        await caller.rollback()

    # The committed row names an unresolved blocker; the decision did not see it.
    assert (
        await db_session.scalar(select(Task.blocked_by_task_id).where(Task.id == task_id))
        == blocker_id
    )
    assert decision.outcome is EngineeringDispatchOutcome.ADMITTED, (
        "SQLAlchemy no longer serves a stale identity-mapped row; the corollary "
        "in engineering_dispatch_admission.py can be revisited"
    )


# --- Invariant A: every condition row is held for the length of the decision -


@pytest.mark.asyncio
async def test_every_condition_row_is_held_for_the_length_of_the_decision(
    async_client: AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
):
    """One probe per row the ladder claims to hold, from a real second transaction.

    The probe runs at the last rung, with rungs 1–4 taken and the paid controls
    still ahead, and tries to write each row a condition read. Every write must
    block: `lock_timeout` turns "somebody holds this" into the error asserted
    here. Read any one of these rows unlocked and its probe commits instead, and
    this test goes red naming that row — the project row is the one that used to.
    """
    import src.engineering_dispatch_admission as admission_module

    telegram_id = await _owner(async_client)
    project_id = await _project(async_client, telegram_id)
    story_id = await _story(async_client, project_id)
    sibling_id = await _task(async_client, project_id, story_id=story_id, title="Sibling")
    task_id = await _task(async_client, project_id, story_id=story_id)

    sibling_run_id = f"eng-sib-{uuid.uuid4().hex[:8]}"
    async with _other_session(db_engine)() as setup:
        setup.add(
            Run(
                id=sibling_run_id,
                type=RunType.ENGINEERING.value,
                status=RunStatus.COMPLETED.value,
                project_id=uuid.UUID(project_id),
                task_id=sibling_id,
                story_id=story_id,
                run_metadata={"iteration": 0},
            )
        )
        await setup.commit()

    probes: dict[str, Callable[[], object]] = {
        "the candidate task": lambda: (
            update(Task).where(Task.id == task_id).values(status="cancelled")
        ),
        "the sibling task": lambda: (
            update(Task).where(Task.id == sibling_id).values(status="in_dev")
        ),
        "the story": lambda: update(Story).where(Story.id == story_id).values(title="moved"),
        "the project": lambda: (
            update(Project)
            .where(Project.id == uuid.UUID(project_id))
            .values(config={"workspace_ready": False})
        ),
        "the sibling's engineering run": lambda: (
            update(Run).where(Run.id == sibling_run_id).values(status=RunStatus.RUNNING.value)
        ),
    }
    held: dict[str, bool] = {}
    original = admission_module.start_paid_run

    async def probe_then_start(command, db):
        for name, statement in probes.items():
            held[name] = await _row_is_held(db_engine, statement())
        return await original(command, db)

    monkeypatch.setattr(admission_module, "start_paid_run", probe_then_start)

    response = await async_client.post(ADMISSION_URL, json={"task_id": task_id})

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.json()["outcome"] == EngineeringDispatchOutcome.ADMITTED
    assert held, "the decision never reached the paid gate"
    unheld = sorted(name for name, was_held in held.items() if not was_held)
    assert not unheld, f"read unlocked: {', '.join(unheld)}"


@pytest.mark.asyncio
async def test_a_sibling_inserted_after_the_roster_peek_ends_the_tick(
    async_client: AsyncClient,
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
):
    """A row lock fences updates, not inserts, and this is the honest answer.

    A task inserted into the story between the roster peek and the locks is a
    sibling the decision does not hold. It cannot be taken now without descending
    the ladder backwards, so the tick refuses with its own typed reason and the
    next one peeks the roster that exists. Nothing is bought in the meantime.
    """
    from src.routers import _story_helpers

    project_id = await _project(async_client, await _owner(async_client))
    story_id = await _story(async_client, project_id)
    task_id = await _task(async_client, project_id, story_id=story_id)

    original = _story_helpers._get_story_for_update
    inserted: list[str] = []

    async def insert_then_lock(wanted_id: str, db: AsyncSession) -> Story:
        if not inserted:
            latecomer = await _task(async_client, project_id, story_id=story_id, title="Latecomer")
            inserted.append(latecomer)
        return await original(wanted_id, db)

    monkeypatch.setattr(_story_helpers, "_get_story_for_update", insert_then_lock)

    decision = await async_client.post(ADMISSION_URL, json={"task_id": task_id})

    assert inserted, "the concurrent insert never ran"
    assert decision.status_code == HTTPStatus.OK, decision.text
    assert decision.json()["reason"] == EngineeringDispatchRefusal.STORY_ROSTER_CHANGED
    assert (await db_session.scalars(select(Run).where(Run.task_id == task_id))).all() == []
