"""A project that predates run ownership cannot create a worker.

The migration that added `Project.initiating_run_id` does not backfill it: a
project created before run ownership existed was created by a run nobody wrote
down, and there is no value that would be true. Rows like that keep NULL, which
this suite reproduces exactly — an upgraded legacy row, not a fresh project.

What is being defended is the label. Anything put in that column is stamped on
every worker the project spawns as `com.codegen.run.id`, so a substitute (the
project id, a freshly minted id, a shared constant) would make two unrelated
later runs on that project answer the same run-scoped query. The API therefore
refuses the request rather than inventing an owner for the worker.
"""

from collections.abc import AsyncGenerator
from http import HTTPStatus
import uuid

from httpx import AsyncClient
import pytest
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Project, Run, Task, TaskEvent, User

LEGACY_TELEGRAM_ID = 999000877


@pytest.fixture
async def legacy_project(db_session: AsyncSession) -> AsyncGenerator[Project, None]:
    """A row exactly as the migration leaves one that predates ownership."""
    owner = (
        await db_session.execute(select(User).where(User.telegram_id == LEGACY_TELEGRAM_ID))
    ).scalar_one_or_none()
    if owner is None:
        owner = User(telegram_id=LEGACY_TELEGRAM_ID, username="legacy_owner", first_name="Legacy")
        db_session.add(owner)
        await db_session.flush()

    suffix = uuid.uuid4().hex[:8]
    project = Project(
        id=uuid.uuid4(),
        title=f"Legacy project {suffix}",
        slug=f"legacy-{suffix}",
        status="active",
        config={"workspace_ready": True},
        owner_id=owner.id,
        initiating_run_id=None,
    )
    db_session.add(project)
    await db_session.commit()
    yield project

    # Tasks reference the project without ON DELETE, so they go first.
    await db_session.execute(
        delete(TaskEvent).where(
            TaskEvent.task_id.in_(select(Task.id).where(Task.project_id == project.id))
        )
    )
    await db_session.execute(delete(Task).where(Task.project_id == project.id))
    await db_session.execute(delete(Project).where(Project.id == project.id))
    await db_session.commit()


async def test_the_legacy_row_really_has_no_run(db_session: AsyncSession, legacy_project: Project):
    """The premise of the rest of the suite, read back from the database.

    If anything ever backfills this column, this assertion is the first thing to
    fail, and it fails on the value that would have been invented.
    """
    stored = await db_session.get(Project, legacy_project.id)
    assert stored is not None
    assert stored.initiating_run_id is None


async def test_spawn_worker_is_refused_and_leaves_nothing_behind(
    async_client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    legacy_project: Project,
):
    """No run to own the worker, so no message, no attempt row, no status move."""
    resp = await async_client.post(
        "/api/tasks/",
        json={
            "project_id": str(legacy_project.id),
            "title": "Work on a project with no run",
            "type": "feature",
        },
    )
    assert resp.status_code == HTTPStatus.CREATED
    task_id = resp.json()["id"]
    status_before = resp.json()["status"]
    queue_len_before = await redis_client.xlen("engineering:queue")

    resp = await async_client.post(f"/api/tasks/{task_id}/spawn-worker", json={"actor": "test"})

    assert resp.status_code == HTTPStatus.CONFLICT
    assert "initiating run" in resp.json()["detail"]
    # The refusal is not a partial start: nothing was published, no engineering
    # attempt was recorded, and the task did not move.
    assert await redis_client.xlen("engineering:queue") == queue_len_before
    runs = (
        (await db_session.execute(select(Run).where(Run.project_id == legacy_project.id)))
        .scalars()
        .all()
    )
    assert runs == []
    resp = await async_client.get(f"/api/tasks/{task_id}")
    assert resp.json()["status"] == status_before


async def test_the_project_is_still_readable(async_client: AsyncClient, legacy_project: Project):
    """Refusing to create workers is the whole compatibility cost.

    A legacy project stays visible and manageable — it just cannot dispatch work
    until it is recreated by a run that names itself. Reading it must not 500 on
    the missing value either, which is why the read schema admits None.
    """
    resp = await async_client.get(f"/api/projects/{legacy_project.id}")

    assert resp.status_code == HTTPStatus.OK
    assert resp.json()["initiating_run_id"] is None
