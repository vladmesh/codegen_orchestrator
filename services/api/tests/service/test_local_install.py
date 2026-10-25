"""Real PostgreSQL/Redis: a product-local INSTALL is stored, owned and never executed."""

import uuid

import pytest
from sqlalchemy import func, select

from shared.contracts.dto.local_install import LocalPackageInstall
from shared.contracts.dto.task import TaskDTO
from shared.models import EngineeringAttemptLedger, EngineeringBudgetReservation, Run, Task
from shared.queues import ENGINEERING_QUEUE, SCAFFOLD_QUEUE, WORKER_COMMANDS


async def post(client, path, body=None, status=200, headers=None):
    result = await client.post(path, json=body, headers=headers)
    assert result.status_code == status, result.text
    return result.json()


async def buyer(client) -> int:
    telegram = uuid.uuid4().int % 1_000_000_000
    await post(client, "/api/users/", {"telegram_id": telegram, "username": "local-owner"}, 201)
    return telegram


async def product(client, telegram, *, managed=True) -> dict:
    project = await post(
        client,
        "/api/projects/",
        {
            "title": "Owned notes product",
            "status": "active",
            "initiating_run_id": f"local-init-{uuid.uuid4().hex[:8]}",
            "config": {"workspace_ready": True, "modules": ["backend", "tg_bot"]},
        },
        201,
        headers={"X-Telegram-ID": str(telegram)},
    )
    repository = await post(
        client,
        "/api/repositories/",
        {
            "project_id": project["id"],
            "name": "Notes",
            "git_url": f"https://github.com/synthetic/notes-{uuid.uuid4().hex[:8]}",
            "is_managed": managed,
        },
        201,
    )
    story = await post(
        client, "/api/stories/", {"project_id": project["id"], "title": "Install"}, 201
    )
    await post(client, f"/api/stories/{story['id']}/start")
    return {"project": project["id"], "repository": repository["id"], "story": story["id"]}


def local_install(owner: dict) -> dict:
    return {
        "route": "package_source",
        "project_id": owner["project"],
        "repository_id": owner["repository"],
        "commit": "1" * 40,
        "path": "packages/notes_digest",
        "package": {
            "name": "notes-digest",
            "distribution": "codegen-kit-notes-digest",
            "version": "0.1.0",
        },
        "core_version": "2.2.0",
        "python_version": "3.12.0",
        "tooling_commit": "2" * 40,
    }


def task_body(owner: dict, *, source: dict | None = None, story: str | None = None) -> dict:
    return {
        "project_id": owner["project"],
        "repository_id": owner["repository"],
        "story_id": story or owner["story"],
        "title": "Install notes digest",
        "type": "install",
        "status": "todo",
        "install": source or local_install(owner),
    }


async def task_count(db_session, project_id: str) -> int:
    return await db_session.scalar(
        select(func.count()).select_from(Task).where(Task.project_id == uuid.UUID(project_id))
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/api/tasks/", "/api/tasks/push"])
async def test_both_create_routes_store_and_read_back_the_exact_local_target(async_client, route):
    owner = await product(async_client, await buyer(async_client))
    created = await post(async_client, route, task_body(owner), 201)
    assert created["install"] == local_install(owner)
    read = TaskDTO.model_validate((await async_client.get(f"/api/tasks/{created['id']}")).json())
    assert isinstance(read.install, LocalPackageInstall)
    assert read.install.model_dump(mode="json") == local_install(owner)
    listed = (await async_client.get("/api/tasks/", params={"story_id": owner["story"]})).json()
    assert [TaskDTO.model_validate(item).install for item in listed] == [read.install]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/api/tasks/", "/api/tasks/push"])
async def test_a_source_of_another_product_is_refused_even_for_the_same_buyer(
    async_client, db_session, route
):
    telegram = await buyer(async_client)
    owner = await product(async_client, telegram)
    other = await product(async_client, telegram)
    unmanaged = await product(async_client, telegram, managed=False)
    for body, code in [
        (
            task_body({**owner, "repository": other["repository"]}),
            "local_install_repository_unowned",
        ),
        (task_body(unmanaged), "local_install_repository_unowned"),
        (task_body(owner, story=other["story"]), "local_install_story_unowned"),
    ]:
        result = await async_client.post(route, json=body)
        assert result.status_code == 409 and code in result.text, result.text
    mismatched = task_body(owner, source=local_install(other))
    assert (await async_client.post(route, json=mismatched)).status_code == 422
    for item in (owner, unmanaged):
        assert await task_count(db_session, item["project"]) == 0


@pytest.mark.asyncio
async def test_a_local_install_keeps_its_ownership_and_payload_immutable(async_client):
    owner = await product(async_client, await buyer(async_client))
    other = await product(async_client, await buyer(async_client))
    task = await post(async_client, "/api/tasks/", task_body(owner), 201)
    for field, value in [
        ("repository_id", other["repository"]),
        ("story_id", other["story"]),
        ("project_id", other["project"]),
    ]:
        result = await async_client.patch(f"/api/tasks/{task['id']}", json={field: value})
        assert result.status_code == 409 and "install_ownership_immutable" in result.text
    result = await async_client.patch(
        f"/api/tasks/{task['id']}", json={"install": local_install(other)}
    )
    assert result.status_code == 422
    assert (await async_client.get(f"/api/tasks/{task['id']}")).json()["install"] == (
        local_install(owner)
    )


@pytest.mark.asyncio
async def test_a_local_install_is_never_admitted_recovered_or_bought(
    async_client, db_session, redis_client
):
    from src.dependencies import create_lk_jwt

    owner = await product(async_client, await buyer(async_client))
    task = await post(async_client, "/api/tasks/", task_body(owner), 201)
    queues = (ENGINEERING_QUEUE, WORKER_COMMANDS, SCAFFOLD_QUEUE)
    before = {queue: await redis_client.xlen(queue) for queue in queues}
    # The kit's own answer for this exact target admits nothing while local installs are off.
    source = {
        "result_version": 1,
        "package": "notes-digest",
        "status": "mechanical",
        "product_core": "2.2.0",
        "target": {
            "route": "package_source",
            "path": "packages/notes_digest",
            "version": "0.1.0",
            "requires_core": ">=2,<3",
            "metadata_sha256": "f" * 64,
        },
        "glue": [],
        "incompatible": None,
    }
    settle = {"operation_id": "op", "token": "t", "stage": "preflight", "preflight": source}
    for body in [
        {"action": "admit"},
        {"action": "claim", "operation_id": "op", "token": "t"},
        {"action": "checkpoint", **settle},
        {"action": "refuse", **settle},
    ]:
        decision = await post(async_client, f"/api/tasks/{task['id']}/catalog-install", body)
        assert (decision["outcome"], decision["reason"]) == ("refused", "local_install_unsupported")
        assert decision["operation"] is None and decision["install"] is None
    admin = await post(
        async_client,
        "/api/users/",
        {"telegram_id": uuid.uuid4().int % 1_000_000_000, "is_admin": True},
        201,
    )
    recovery = await async_client.post(
        f"/api/tasks/{task['id']}/catalog-install/recovery",
        headers={"X-Internal-Key": "", "Authorization": f"Bearer {create_lk_jwt(admin['id'])}"},
        json={"operation_id": "op", "action": "retry"},
    )
    assert recovery.status_code == 409 and "local_install_unsupported" in recovery.text
    for path, body in [
        ("/api/work-admission/engineering-dispatches", {"task_id": task["id"]}),
        (
            "/api/work-admission/paid-runs",
            {
                "id": f"forbidden-{uuid.uuid4().hex}",
                "type": "engineering",
                "task_id": task["id"],
                "story_id": task["story_id"],
                "project_id": task["project_id"],
            },
        ),
        (f"/api/tasks/{task['id']}/spawn-worker", {}),
    ]:
        response = await async_client.post(path, json=body)
        assert "catalog_install_not_engineering" in response.text.replace(" ", "_"), response.text
    read = (await async_client.get(f"/api/tasks/{task['id']}")).json()
    assert (read["status"], read["install_operation"]) == ("todo", None)
    project = uuid.UUID(task["project_id"])
    for model, column in [
        (Run, Run.project_id),
        (EngineeringAttemptLedger, EngineeringAttemptLedger.project_id),
        (EngineeringBudgetReservation, EngineeringBudgetReservation.project_id),
    ]:
        count = select(func.count()).select_from(model).where(column == project)
        assert await db_session.scalar(count) == 0
    assert before == {queue: await redis_client.xlen(queue) for queue in queues}
