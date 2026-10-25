"""Real PostgreSQL/Redis: a product-local INSTALL is owned, settled and never bought."""

from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy import func, select

from shared.contracts.dto.local_install import LocalInstallVerification, LocalPackageInstall
from shared.contracts.dto.task import TaskDTO
from shared.models import EngineeringAttemptLedger, EngineeringBudgetReservation, Run, Story, Task
from shared.queues import ENGINEERING_QUEUE, SCAFFOLD_QUEUE, WORKER_COMMANDS
from shared.tests.unit.test_local_verification import verification


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


def source_preflight(status="mechanical", glue=()) -> dict:
    """The kit's `check-install --package-source` answer for `local_install`."""
    return {
        "result_version": 1,
        "package": "notes-digest",
        "status": status,
        "product_core": "2.2.0",
        "target": {
            "route": "package_source",
            "path": "packages/notes_digest",
            "version": "0.1.0",
            "requires_core": ">=2,<3",
            "metadata_sha256": "4" * 64,
        },
        "glue": list(glue),
        "incompatible": None,
    }


def proof(owner: dict, mode="provenance", **changes) -> dict:
    """The fixed probe's `LocalInstallVerification` of `local_install(owner)`."""
    return verification(mode, **{"target": local_install(owner), **changes})


RELEASED_PROOF = {
    "core_version": "2.2.0",
    "tooling_commit": "2" * 40,
    "binding_sha256": "4" * 64,
    "distributions": {"notes-digest": "0.1.0"},
    "component_targets": {"notes-digest": "3" * 40},
    "protected_sha256": {},
}
INSTALL_HEAD = "9" * 40
OTHER_OWNER = "different"


async def command(client, task, action, **fields):
    return await post(
        client, f"/api/tasks/{task['id']}/catalog-install", {"action": action, **fields}
    )


async def claimed(client, task) -> dict:
    admitted = await command(client, task, "admit")
    assert admitted["outcome"] == "admitted", admitted
    identity = {"operation_id": admitted["operation"]["id"], "token": "owner"}
    assert (await command(client, task, "claim", **identity))["outcome"] == "claimed"
    return identity


async def stored_operation(db_session, task) -> dict | None:
    return await db_session.scalar(select(Task.install_operation).where(Task.id == task["id"]))


async def no_paid_work(db_session, task):
    project = uuid.UUID(task["project_id"])
    for model, column in [
        (Run, Run.project_id),
        (EngineeringAttemptLedger, EngineeringAttemptLedger.project_id),
        (EngineeringBudgetReservation, EngineeringBudgetReservation.project_id),
    ]:
        count = select(func.count()).select_from(model).where(column == project)
        assert await db_session.scalar(count) == 0


@pytest.mark.asyncio
async def test_a_local_install_is_admitted_proven_and_published_once(
    async_client, db_session, redis_client
):
    owner = await product(async_client, await buyer(async_client))
    task = await post(async_client, "/api/tasks/", task_body(owner), 201)
    queues = (ENGINEERING_QUEUE, WORKER_COMMANDS, SCAFFOLD_QUEUE)
    before = {queue: await redis_client.xlen(queue) for queue in queues}

    admitted = await command(async_client, task, "admit")
    assert admitted["install"] == local_install(owner)
    identity = {"operation_id": admitted["operation"]["id"], "token": "owner"}
    await command(async_client, task, "claim", **identity)
    checkout = f"{owner['repository']}/{identity['operation_id']}"
    await command(
        async_client,
        task,
        "checkpoint",
        stage="preflight",
        checkout=checkout,
        base_sha="1" * 40,
        preflight=source_preflight(),
        verification=proof(owner),
        **identity,
    )
    unread = await command(async_client, task, "publish", head_sha=INSTALL_HEAD, **identity)
    assert unread["reason"] == "head_unverified"
    await command(
        async_client, task, "checkpoint", stage="commit", head_sha=INSTALL_HEAD, **identity
    )
    provenance_only = await command(
        async_client, task, "publish", head_sha=INSTALL_HEAD, **identity
    )
    assert provenance_only["reason"] == "readback_unverified"
    readback = {"stage": "readback", "verification": proof(owner, "readback"), **identity}
    first = await command(async_client, task, "checkpoint", **readback)
    # A lost response repeats the same readback: it is kept once, not refused as changed.
    replay = await command(async_client, task, "checkpoint", **readback)
    assert first["outcome"] == replay["outcome"] == "reused"
    assert replay["operation"]["verification"] == proof(owner, "readback")
    stored = await stored_operation(db_session, task)
    assert stored == replay["operation"]
    read = TaskDTO.model_validate((await async_client.get(f"/api/tasks/{task['id']}")).json())
    assert isinstance(read.install_operation.verification, LocalInstallVerification)
    assert read.install_operation.model_dump(mode="json") == stored

    published = await command(async_client, task, "publish", head_sha=INSTALL_HEAD, **identity)
    operation = published["operation"]
    assert (published["outcome"], operation["state"]) == ("settled", "published")
    assert (operation["base_sha"], operation["head_sha"]) == ("1" * 40, INSTALL_HEAD)
    assert operation["verification"]["head"] == "1" * 40
    assert (await async_client.get(f"/api/tasks/{task['id']}")).json()["status"] == "done"
    events = (await async_client.get(f"/api/tasks/{task['id']}/events")).json()
    again = await command(async_client, task, "claim", **identity)
    assert again["operation"] == operation
    assert (await async_client.get(f"/api/tasks/{task['id']}/events")).json() == events
    await no_paid_work(db_session, task)
    assert before == {queue: await redis_client.xlen(queue) for queue in queues}


@pytest.mark.asyncio
async def test_wrong_route_changed_or_foreign_proof_is_refused_before_it_is_kept(
    async_client, db_session
):
    telegram = await buyer(async_client)
    owner = await product(async_client, telegram)
    other = await product(async_client, telegram)
    task = await post(async_client, "/api/tasks/", task_body(owner), 201)
    identity = await claimed(async_client, task)
    foreign = proof(owner, target=local_install(other))
    catalog_answer = source_preflight() | {
        "target": {
            "route": "catalog",
            "catalog_source": "https://github.com/vladmesh/codegen-product-kit.git",
            "catalog_ref": "d" * 40,
            "tag": "packages/notes-digest/v0.1.0",
            "version": "0.1.0",
            "requires_core": ">=2,<3",
            "metadata_sha256": "4" * 64,
        }
    }
    moved = proof(owner, "readback", source={"tree": "8" * 40})
    for fields, reason in [
        ({"verification": proof(owner, "readback")}, "verification_provenance_missing"),
        ({"verification": RELEASED_PROOF}, "verification_route_mismatch"),
        ({"verification": foreign}, "verification_mismatch"),
        ({"verification": proof(owner, head="8" * 40)}, "verification_mismatch"),
        ({"preflight": catalog_answer}, "preflight_provenance_mismatch"),
        ({"base_sha": "8" * 40}, "base_mismatch"),
    ]:
        refused = await command(async_client, task, "checkpoint", **fields, **identity)
        assert (refused["outcome"], refused["reason"]) == ("refused", reason)
    assert (await stored_operation(db_session, task))["verification"] is None

    await command(async_client, task, "checkpoint", verification=proof(owner), **identity)
    changed = await command(async_client, task, "checkpoint", verification=moved, **identity)
    assert changed["reason"] == "verification_mismatch"
    stolen = await command(
        async_client,
        task,
        "checkpoint",
        verification=proof(owner, "readback"),
        operation_id=identity["operation_id"],
        token=OTHER_OWNER,
    )
    assert stolen["reason"] == "lease_lost"
    assert (await stored_operation(db_session, task))["verification"] == proof(owner)

    await command(
        async_client,
        task,
        "checkpoint",
        head_sha=INSTALL_HEAD,
        verification=proof(owner, "readback"),
        **identity,
    )
    regressed = await command(
        async_client, task, "checkpoint", verification=proof(owner), **identity
    )
    assert regressed["reason"] == "verification_changed"
    # A readback is no installation without the exact base and the kit's mechanical answer.
    unbased = await command(async_client, task, "publish", head_sha=INSTALL_HEAD, **identity)
    assert unbased["reason"] == "base_unverified"
    await command(async_client, task, "checkpoint", base_sha="1" * 40, **identity)
    unadmitted = await command(async_client, task, "publish", head_sha=INSTALL_HEAD, **identity)
    assert unadmitted["reason"] == "preflight_unverified"
    assert (await async_client.get(f"/api/tasks/{task['id']}")).json()["status"] == "in_dev"


@pytest.mark.asyncio
async def test_local_glue_parks_for_a_person_without_an_engineering_repair(async_client):
    owner = await product(async_client, await buyer(async_client))
    task = await post(async_client, "/api/tasks/", task_body(owner), 201)
    identity = await claimed(async_client, task)
    answer = source_preflight(
        "glue",
        [
            {
                "code": "command_conflict",
                "path": "services/tg_bot/src/commands.py",
                "line": 7,
                "owner": "product",
                "symbol": "handle_digest",
                "key": None,
                "command": "digest",
                "conflict": "/digest is already registered by the product",
                "action": "rename the product command",
                "other": None,
            }
        ],
    )
    settled = await command(
        async_client,
        task,
        "refuse",
        stage="preflight",
        detail="glue_required: command_conflict at services/tg_bot/src/commands.py:7",
        preflight=answer,
        **identity,
    )
    operation = settled["operation"]
    assert (settled["outcome"], operation["state"]) == ("settled", "refused")
    assert operation["preflight"] == answer
    read = (await async_client.get(f"/api/tasks/{task['id']}")).json()
    assert (read["status"], read["blocked_by_task_id"]) == ("waiting_human_review", None)
    assert read["install_operation"] == operation
    story = (await async_client.get(f"/api/stories/{owner['story']}")).json()
    assert story["status"] == "waiting_human_review"
    tasks = (await async_client.get("/api/tasks/", params={"story_id": owner["story"]})).json()
    assert [item["type"] for item in tasks] == ["install"]
    events = (await async_client.get(f"/api/tasks/{task['id']}/events")).json()
    assert not any("glue_repair_task_id" in event["details"] for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", ["reopened", "stopped"])
async def test_a_stale_or_stopped_owner_keeps_no_local_proof(async_client, db_session, fence):
    owner = await product(async_client, await buyer(async_client))
    task = await post(async_client, "/api/tasks/", task_body(owner), 201)
    identity = await claimed(async_client, task)
    if fence == "reopened":
        story = await db_session.get(Story, owner["story"])
        story.reopened_at = datetime.now(UTC)
        await db_session.commit()
    else:
        await post(async_client, f"/api/stories/{owner['story']}/human-review")
    answer = await command(async_client, task, "checkpoint", verification=proof(owner), **identity)
    assert answer["outcome"] == "refused"
    if fence == "reopened":
        assert answer["reason"] == "stale_cycle"
    assert (await stored_operation(db_session, task))["verification"] is None


@pytest.mark.asyncio
async def test_a_target_naming_another_product_of_the_same_buyer_is_never_admitted(
    async_client, db_session
):
    telegram = await buyer(async_client)
    owner = await product(async_client, telegram)
    other = await product(async_client, telegram)
    task = await post(async_client, "/api/tasks/", task_body(owner), 201)
    row = await db_session.get(Task, task["id"])
    row.install = {**local_install(owner), "repository_id": other["repository"]}
    await db_session.commit()
    result = await async_client.post(
        f"/api/tasks/{task['id']}/catalog-install", json={"action": "admit"}
    )
    assert result.status_code == 409 and "repository_unowned" in result.text
    assert await stored_operation(db_session, task) is None


@pytest.mark.asyncio
async def test_lost_local_lease_keeps_its_proof_and_operator_recovery_stays_refused(
    async_client, db_session
):
    from src.dependencies import create_lk_jwt

    owner = await product(async_client, await buyer(async_client))
    task = await post(async_client, "/api/tasks/", task_body(owner), 201)
    identity = await claimed(async_client, task)
    await command(async_client, task, "checkpoint", verification=proof(owner), **identity)
    row = await db_session.get(Task, task["id"])
    row.install_operation = {
        **row.install_operation,
        "heartbeat_at": (datetime.now(UTC) - timedelta(minutes=20)).isoformat(),
    }
    await db_session.commit()
    parked = await command(async_client, task, "admit")
    operation = parked["operation"]
    assert (operation["state"], operation["stage"]) == ("recovery_required", "lease_lost")
    assert operation["verification"] == proof(owner)
    admin = await post(
        async_client,
        "/api/users/",
        {"telegram_id": uuid.uuid4().int % 1_000_000_000, "is_admin": True},
        201,
    )
    for action in ("retry", "recover", "replan"):
        recovery = await async_client.post(
            f"/api/tasks/{task['id']}/catalog-install/recovery",
            headers={"X-Internal-Key": "", "Authorization": f"Bearer {create_lk_jwt(admin['id'])}"},
            json={"operation_id": operation["id"], "action": action},
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
    assert (read["status"], read["install_operation"]) == ("waiting_human_review", operation)
    await no_paid_work(db_session, task)
