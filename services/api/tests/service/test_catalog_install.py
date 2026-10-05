"""Real PostgreSQL/Redis ownership and zero engineering accounting."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
import uuid

import pytest
from sqlalchemy import func, select

from shared.contracts.dto.task import TaskDTO
from shared.models import EngineeringAttemptLedger, EngineeringBudgetReservation, Run, Story, Task
from shared.queues import ENGINEERING_QUEUE, WORKER_COMMANDS

LEASE_OWNER = "owner"
OTHER_OWNER = "different"


@pytest.mark.asyncio
async def test_expired_cancelled_old_cycle_can_be_reconciled_without_changing_new_story(
    install_task, async_client, db_session
):
    from src.dependencies import create_lk_jwt

    admission = await command(async_client, install_task, "admit")
    oid = admission["operation"]["id"]
    await command(async_client, install_task, "claim", operation_id=oid, token=LEASE_OWNER)
    await async_client.delete(f"/api/tasks/{install_task['id']}")
    task = await db_session.get(Task, install_task["id"])
    task.install_operation = {
        **task.install_operation,
        "heartbeat_at": (datetime.now(UTC) - timedelta(minutes=20)).isoformat(),
    }
    story = await db_session.get(Story, install_task["story_id"])
    story.reopened_at = datetime.now(UTC)
    await db_session.commit()
    before = (await async_client.get(f"/api/stories/{story.id}")).json()
    settled = await command(async_client, install_task, "admit")
    assert settled["operation"]["state"] == "recovery_required"
    after = (await async_client.get(f"/api/stories/{story.id}")).json()
    assert {key: after[key] for key in ("status", "reopened_at", "quarantine_reason")} == {
        key: before[key] for key in ("status", "reopened_at", "quarantine_reason")
    }
    admin = await post(
        async_client,
        "/api/users/",
        {
            "telegram_id": uuid.uuid4().int % 1_000_000_000,
            "is_admin": True,
        },
        201,
    )
    result = await async_client.post(
        f"/api/tasks/{task.id}/catalog-install/recovery",
        headers={"X-Internal-Key": "", "Authorization": f"Bearer {create_lk_jwt(admin['id'])}"},
        json={"operation_id": oid, "action": "retry"},
    )
    assert result.status_code == 200, result.text
    assert result.json()["operation"]["state"] == "refused"
    after = (await async_client.get(f"/api/stories/{story.id}")).json()
    assert {key: after[key] for key in ("status", "reopened_at", "quarantine_reason")} == {
        key: before[key] for key in ("status", "reopened_at", "quarantine_reason")
    }
    assert (await async_client.get(f"/api/tasks/{task.id}")).json()["status"] == "cancelled"


def payload():
    return {
        "package": {
            "name": "reminders",
            "distribution": "codegen-kit-reminders",
            "version": "0.5.0",
            "tag": "packages/reminders/v0.5.0",
        },
        "libraries": [
            {
                "name": "textparse",
                "distribution": "codegen-kit-textparse",
                "version": "0.1.0",
                "tag": "packages/textparse/v0.1.0",
            }
        ],
        "binding": {
            "package": "reminders",
            "resource": "codegen_kit_reminders:bindings/default.yaml",
            "sha256": "a" * 64,
            "functions": ["textparse.when"],
        },
        "core_version": "2.2.0",
        "python_version": "3.12.0",
        "catalog_digest": "b" * 64,
        "tooling_commit": "c" * 40,
    }


async def post(client, path, body=None, status=200):
    result = await client.post(path, json=body)
    assert result.status_code == status, result.text
    return result.json()


@pytest.fixture
async def install_task(async_client):
    telegram = uuid.uuid4().int % 1_000_000_000
    await post(
        async_client, "/api/users/", {"telegram_id": telegram, "username": "install-owner"}, 201
    )
    response = await async_client.post(
        "/api/projects/",
        headers={"X-Telegram-ID": str(telegram)},
        json={
            "title": "Owned notes product",
            "status": "active",
            "initiating_run_id": "install-init",
            "config": {"workspace_ready": True, "modules": ["backend", "tg_bot"]},
        },
    )
    assert response.status_code == 201, response.text
    project = response.json()["id"]
    repo = await post(
        async_client,
        "/api/repositories/",
        {
            "project_id": project,
            "name": "Notes",
            "git_url": f"https://github.com/synthetic/notes-{telegram}",
        },
        201,
    )
    story = await post(
        async_client, "/api/stories/", {"project_id": project, "title": "Install"}, 201
    )
    await post(async_client, f"/api/stories/{story['id']}/start")
    task = await post(
        async_client,
        "/api/tasks/",
        {
            "project_id": project,
            "repository_id": repo["id"],
            "story_id": story["id"],
            "title": "Install reminders",
            "type": "install",
            "status": "todo",
            "install": payload(),
        },
        201,
    )
    yield task


async def command(client, task, action, **fields):
    return await post(
        client, f"/api/tasks/{task['id']}/catalog-install", {"action": action, **fields}
    )


@pytest.mark.asyncio
async def test_paid_and_manual_paths_leave_no_run_ledger_budget_or_worker(
    install_task, async_client, db_session, redis_client
):
    task = install_task
    before = {
        queue: await redis_client.xlen(queue) for queue in (ENGINEERING_QUEUE, WORKER_COMMANDS)
    }
    admitted = await command(async_client, task, "admit")
    assert admitted["outcome"] == "admitted"
    read = TaskDTO.model_validate((await async_client.get(f"/api/tasks/{task['id']}")).json())
    assert read.type == "install" and read.install.model_dump(mode="json") == payload()
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
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Run).where(Run.task_id == task["id"])
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(EngineeringAttemptLedger)
            .where(EngineeringAttemptLedger.project_id == uuid.UUID(task["project_id"]))
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(EngineeringBudgetReservation)
            .where(EngineeringBudgetReservation.project_id == uuid.UUID(task["project_id"]))
        )
        == 0
    )
    assert before == {queue: await redis_client.xlen(queue) for queue in before}


@pytest.mark.asyncio
async def test_concurrent_claim_and_response_loss_replay_have_one_writer(
    install_task, async_client
):
    admitted = await command(async_client, install_task, "admit")
    operation = admitted["operation"]["id"]
    responses = await asyncio.gather(
        *(
            command(async_client, install_task, "claim", operation_id=operation, token=token)
            for token in ("writer-1", "writer-2")
        )
    )
    winners = [item for item in responses if item["outcome"] == "claimed"]
    assert len(winners) == 1
    winner = winners[0]["operation"]
    replay = await command(
        async_client, install_task, "claim", operation_id=operation, token=winner["token"]
    )
    assert replay["operation"] == winner
    loser = await command(
        async_client,
        install_task,
        "heartbeat",
        operation_id=operation,
        token=OTHER_OWNER,
    )
    assert loser["reason"] == "lease_lost"


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", ["coverage", "blocker", "cancelled", "stopped"])
async def test_dispatch_fences_precede_claim(install_task, async_client, db_session, fence):
    task = await db_session.get(Task, install_task["id"])
    if fence == "coverage":
        task.dispatch_admitted = False
    elif fence == "blocker":
        blocker = await post(
            async_client,
            "/api/tasks/",
            {
                "project_id": install_task["project_id"],
                "title": "Unresolved predecessor",
                "status": "todo",
            },
            201,
        )
        task.blocked_by_task_id = blocker["id"]
    elif fence == "cancelled":
        task.status = "cancelled"
    else:
        await post(async_client, f"/api/stories/{install_task['story_id']}/human-review")
    await db_session.commit()
    answer = await command(async_client, install_task, "admit")
    assert answer["outcome"] == "refused"
    assert answer["operation"] is None


@pytest.mark.asyncio
async def test_expired_execution_is_parked_once_without_an_engineering_retry(
    install_task, async_client, db_session
):
    admission = await command(async_client, install_task, "admit")
    operation = admission["operation"]["id"]
    await command(async_client, install_task, "claim", operation_id=operation, token=LEASE_OWNER)
    task = await db_session.get(Task, install_task["id"])
    stored = dict(task.install_operation)
    stored["heartbeat_at"] = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
    task.install_operation = stored
    await db_session.commit()
    settled = await command(async_client, install_task, "admit")
    assert settled["operation"]["state"] == "recovery_required"
    assert settled["operation"]["stage"] == "lease_lost"
    replay = await command(async_client, install_task, "admit")
    assert replay["operation"] == settled["operation"]
    assert (await async_client.get(f"/api/tasks/{install_task['id']}")).json()[
        "status"
    ] == "waiting_human_review"
    assert (
        await db_session.scalar(select(func.count()).select_from(Run).where(Run.task_id == task.id))
        == 0
    )


@pytest.mark.asyncio
async def test_failure_retains_the_exact_head_and_only_parks_its_story(install_task, async_client):
    admitted = await command(async_client, install_task, "admit")
    identity = {"operation_id": admitted["operation"]["id"], "token": "owner"}
    await command(async_client, install_task, "claim", **identity)
    settled = await command(
        async_client,
        install_task,
        "refuse",
        stage="push",
        head_sha="d" * 40,
        detail="push_outcome_unknown",
        **identity,
    )
    assert settled["operation"]["head_sha"] == "d" * 40
    assert settled["operation"]["state"] == "recovery_required"
    story = (await async_client.get(f"/api/stories/{install_task['story_id']}")).json()
    assert story["status"] == "waiting_human_review"
    assert admitted["operation"]["id"] in story["quarantine_reason"]["detail"]


@pytest.mark.asyncio
async def test_publish_requires_verified_closure_and_replays_without_status_events(
    install_task, async_client
):
    admitted = await command(async_client, install_task, "admit")
    identity = {"operation_id": admitted["operation"]["id"], "token": "owner"}
    await command(async_client, install_task, "claim", **identity)
    refused = await command(async_client, install_task, "publish", head_sha="d" * 40, **identity)
    assert refused["reason"] == "head_unverified"
    verification = {
        "core_version": "2.2.0",
        "tooling_commit": "c" * 40,
        "binding_sha256": "a" * 64,
        "distributions": {"reminders": "0.5.0", "textparse": "0.1.0"},
        "component_targets": {"reminders": "e" * 40, "textparse": "f" * 40},
        "protected_sha256": {},
    }
    await command(
        async_client,
        install_task,
        "checkpoint",
        stage="push",
        head_sha="d" * 40,
        base_sha="b" * 40,
        verification=verification,
        **identity,
    )
    published = await command(async_client, install_task, "publish", head_sha="d" * 40, **identity)
    assert published["operation"]["state"] == "published"
    assert (await async_client.get(f"/api/tasks/{install_task['id']}")).json()["status"] == "done"
    events = (await async_client.get(f"/api/tasks/{install_task['id']}/events")).json()
    replay = await command(async_client, install_task, "claim", **identity)
    assert replay["operation"] == published["operation"]
    assert events == (await async_client.get(f"/api/tasks/{install_task['id']}/events")).json()


@pytest.mark.asyncio
async def test_cancelled_queued_operation_releases_without_starting(install_task, async_client):
    admission = await command(async_client, install_task, "admit")
    cancelled = await async_client.delete(f"/api/tasks/{install_task['id']}")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["install_operation"]["state"] == "refused"
    assert cancelled.json()["install_operation"]["stage"] == "cancelled"
    claim = await command(
        async_client,
        install_task,
        "claim",
        operation_id=admission["operation"]["id"],
        token=LEASE_OWNER,
    )
    assert claim["outcome"] == "settled" and claim["operation"]["state"] == "refused"


@pytest.mark.asyncio
async def test_cancelled_running_owner_retains_head_and_cannot_publish(install_task, async_client):
    admission = await command(async_client, install_task, "admit")
    identity = {"operation_id": admission["operation"]["id"], "token": "owner"}
    await command(async_client, install_task, "claim", **identity)
    await command(
        async_client, install_task, "checkpoint", stage="push", head_sha="d" * 40, **identity
    )
    cancelled = await async_client.delete(f"/api/tasks/{install_task['id']}")
    assert cancelled.status_code == 200, cancelled.text
    answer = await command(async_client, install_task, "publish", head_sha="d" * 40, **identity)
    assert answer["outcome"] == "refused" and answer["reason"] == "task_not_dispatchable"
    settled = await command(async_client, install_task, "refuse", stage="cancelled", **identity)
    assert settled["operation"]["state"] == "recovery_required"
    assert settled["operation"]["head_sha"] == "d" * 40
    assert (await async_client.get(f"/api/tasks/{install_task['id']}")).json()[
        "status"
    ] == "cancelled"


@pytest.mark.asyncio
async def test_generic_mutations_cannot_replace_install_ownership(install_task, async_client):
    for suffix, body in [
        ("start", {}),
        ("complete", {}),
        ("fail", {}),
        ("resume", {"retries": 1, "guidance": "Reviewed install recovery"}),
        ("transition?to_status=waiting_human_review", {}),
    ]:
        result = await async_client.post(f"/api/tasks/{install_task['id']}/{suffix}", json=body)
        assert result.status_code == 409, result.text
        assert "catalog_install_requires_owned_settlement" in result.text
    result = await async_client.patch(
        f"/api/tasks/{install_task['id']}", json={"repository_id": None}
    )
    assert result.status_code == 409 and "install_ownership_immutable" in result.text
    assert (await async_client.get(f"/api/tasks/{install_task['id']}")).json()["status"] == "todo"


@pytest.mark.asyncio
@pytest.mark.parametrize("published", [True, False])
async def test_admin_recovery_reads_exact_retained_head_and_never_buys_work(
    install_task, async_client, published
):
    from src.dependencies import create_lk_jwt

    admission = await command(async_client, install_task, "admit")
    operation = admission["operation"]["id"]
    identity = {"operation_id": operation, "token": "owner"}
    await command(async_client, install_task, "claim", **identity)
    proof = {
        "core_version": "2.2.0",
        "tooling_commit": "c" * 40,
        "binding_sha256": "a" * 64,
        "distributions": {"reminders": "0.5.0", "textparse": "0.1.0"},
        "component_targets": {"reminders": "e" * 40, "textparse": "f" * 40},
        "protected_sha256": {},
    }
    await command(
        async_client,
        install_task,
        "checkpoint",
        stage="push",
        head_sha="d" * 40,
        verification=proof,
        **identity,
    )
    await command(
        async_client,
        install_task,
        "refuse",
        stage="push",
        detail="push_outcome_unknown",
        **identity,
    )
    telegram = uuid.uuid4().int % 1_000_000_000
    admin = await post(
        async_client, "/api/users/", {"telegram_id": telegram, "is_admin": True}, 201
    )
    github = AsyncMock()
    github.__aenter__.return_value = github
    github.get_ref_sha.return_value = "d" * 40 if published else "e" * 40
    with patch("src.catalog_install_recovery.GitHubAppClient", return_value=github):
        result = await async_client.post(
            f"/api/tasks/{install_task['id']}/catalog-install/recovery",
            headers={"X-Internal-Key": "", "Authorization": f"Bearer {create_lk_jwt(admin['id'])}"},
            json={"operation_id": operation, "action": "recover"},
        )
    assert result.status_code == (200 if published else 409), result.text
    github.get_ref_sha.assert_awaited_once()
    task = (await async_client.get(f"/api/tasks/{install_task['id']}")).json()
    assert task["status"] == ("done" if published else "waiting_human_review")
    assert task["install_operation"]["head_sha"] == "d" * 40
