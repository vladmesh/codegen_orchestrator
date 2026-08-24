"""Service coverage for canonical engineering-attempt accounting."""

import asyncio
from http import HTTPStatus
from unittest.mock import AsyncMock
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from shared.models import EngineeringAttemptLedger, EngineeringBudgetReservation, Task
from shared.redis.client import RedisStreamClient


async def _user(client: AsyncClient, telegram_id: int, *, is_admin: bool = False) -> dict:
    response = await client.post(
        "/api/users/",
        json={
            "telegram_id": telegram_id,
            "username": f"ledger_{telegram_id}",
            "is_admin": is_admin,
        },
    )
    assert response.status_code == HTTPStatus.CREATED
    return response.json()


async def _project(client: AsyncClient, telegram_id: int) -> dict:
    response = await client.post(
        "/api/projects/",
        json={
            "id": str(uuid.uuid4()),
            "title": "Ledger ownership",
            "initiating_run_id": f"init-{uuid.uuid4().hex}",
            "config": {},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert response.status_code == HTTPStatus.CREATED, response.text
    return response.json()


@pytest.mark.asyncio
async def test_project_bound_run_uses_project_owner_not_supplied_user(async_client: AsyncClient):
    owner = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    other = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    project = await _project(async_client, owner["telegram_id"])

    response = await async_client.post(
        "/api/runs/",
        json={
            "id": f"eng-{uuid.uuid4().hex}",
            "type": "engineering",
            "project_id": project["id"],
            "user_id": other["id"],
        },
    )

    assert response.status_code == HTTPStatus.CREATED
    assert response.json()["user_id"] == owner["id"]

    rewrite = await async_client.patch(
        f"/api/runs/{response.json()['id']}", json={"user_id": other["id"]}
    )
    assert rewrite.status_code == HTTPStatus.CONFLICT
    persisted = await async_client.get(f"/api/runs/{response.json()['id']}")
    assert persisted.json()["user_id"] == owner["id"]


@pytest.mark.asyncio
async def test_terminal_engineering_run_writes_one_unknown_cost_ledger_row(
    async_client: AsyncClient,
):
    owner = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    project = await _project(async_client, owner["telegram_id"])
    run_id = f"eng-{uuid.uuid4().hex}"
    created = await async_client.post(
        "/api/runs/",
        json={"id": run_id, "type": "engineering", "project_id": project["id"]},
    )
    assert created.status_code == HTTPStatus.CREATED
    terminal = {
        "status": "failed",
        "error_message": "timed out",
        "engineering_attempt": {
            "provider": "anthropic",
            "model": "claude-sonnet",
            "input_tokens": 10,
            "output_tokens": 5,
            "cost_source": "unknown",
        },
    }
    first = await async_client.patch(f"/api/runs/{run_id}", json=terminal)
    second = await async_client.patch(f"/api/runs/{run_id}", json=terminal)
    assert first.status_code == HTTPStatus.OK
    assert second.status_code == HTTPStatus.OK

    rows = await async_client.get("/api/runs/engineering-attempts", params={"run_id": run_id})
    assert rows.status_code == HTTPStatus.OK
    assert len(rows.json()) == 1
    row = rows.json()[0]
    assert row["idempotency_key"] == f"engineering-run:{run_id}"
    assert row["user_id"] == owner["id"]
    assert row["total_tokens"] == 15
    assert row["cost_source"] == "unknown"
    assert row["cost_microusd"] is None

    changed_retry = {
        **terminal,
        "engineering_attempt": {
            "provider": "other-provider",
            "model": "other-model",
            "total_tokens": 999,
            "cost_source": "unknown",
        },
    }
    assert (
        await async_client.patch(f"/api/runs/{run_id}", json=changed_retry)
    ).status_code == HTTPStatus.OK
    rows_after_retry = await async_client.get(
        "/api/runs/engineering-attempts", params={"run_id": run_id}
    )
    assert rows_after_retry.json() == rows.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled"])
async def test_terminal_engineering_run_preserves_provider_reported_cost(
    async_client: AsyncClient, terminal_status: str
):
    owner = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    project = await _project(async_client, owner["telegram_id"])
    run_id = f"eng-provider-{uuid.uuid4().hex}"
    await async_client.post(
        "/api/runs/",
        json={"id": run_id, "type": "engineering", "project_id": project["id"]},
    )
    terminal = {
        "status": terminal_status,
        "engineering_attempt": {
            "claude_evidence": {
                "provider": "anthropic",
                "model": "claude-sonnet",
                "input_tokens": 12,
                "output_tokens": 5,
                "total_tokens": 17,
                "cache_read_tokens": 4,
                "cache_write_tokens": 3,
                "cost_microusd": 40_001,
            }
        },
    }
    assert (
        await async_client.patch(f"/api/runs/{run_id}", json=terminal)
    ).status_code == HTTPStatus.OK
    assert (
        await async_client.patch(f"/api/runs/{run_id}", json=terminal)
    ).status_code == HTTPStatus.OK

    rows = await async_client.get("/api/runs/engineering-attempts", params={"run_id": run_id})
    assert rows.status_code == HTTPStatus.OK
    assert rows.json()[0]["cost_microusd"] == 40_001
    assert rows.json()[0]["cost_source"] == "provider_reported"
    assert rows.json()[0]["cache_read_tokens"] == 4
    assert rows.json()[0]["cache_write_tokens"] == 3
    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["total_tokens"] == 17
    assert run.json()["cost_usd"] == pytest.approx(0.040001)


@pytest.mark.asyncio
async def test_ledger_filters_and_owner_authorization(async_client: AsyncClient):
    owner = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    intruder = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    project = await _project(async_client, owner["telegram_id"])
    run_id = f"eng-{uuid.uuid4().hex}"
    await async_client.post(
        "/api/runs/",
        json={"id": run_id, "type": "engineering", "project_id": project["id"]},
    )
    await async_client.patch(f"/api/runs/{run_id}", json={"status": "cancelled"})

    own = await async_client.get(
        "/api/runs/engineering-attempts",
        params={"project_id": project["id"], "run_id": run_id},
        headers={"X-Telegram-ID": str(owner["telegram_id"])},
    )
    assert own.status_code == HTTPStatus.OK
    assert [row["run_id"] for row in own.json()] == [run_id]
    forbidden = await async_client.get(
        "/api/runs/engineering-attempts",
        params={"run_id": run_id},
        headers={"X-Telegram-ID": str(intruder["telegram_id"])},
    )
    assert forbidden.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.asyncio
async def test_ledger_read_is_bounded(async_client: AsyncClient):
    owner = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    project = await _project(async_client, owner["telegram_id"])
    for _ in range(2):
        run_id = f"eng-page-{uuid.uuid4().hex}"
        await async_client.post(
            "/api/runs/",
            json={"id": run_id, "type": "engineering", "project_id": project["id"]},
        )
        assert (
            await async_client.patch(f"/api/runs/{run_id}", json={"status": "cancelled"})
        ).status_code == HTTPStatus.OK

    page = await async_client.get(
        "/api/runs/engineering-attempts",
        params={"project_id": project["id"], "limit": 1},
    )
    assert page.status_code == HTTPStatus.OK
    assert len(page.json()) == 1


@pytest.mark.asyncio
async def test_project_deletion_detaches_but_retains_engineering_ledger(
    async_client: AsyncClient, db_session
):
    """Hard project deletion retains consumed-resource history without live links."""
    owner = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    project = await _project(async_client, owner["telegram_id"])
    run_id = f"eng-delete-{uuid.uuid4().hex}"
    await async_client.post(
        "/api/runs/",
        json={"id": run_id, "type": "engineering", "project_id": project["id"]},
    )
    await async_client.patch(
        f"/api/runs/{run_id}",
        json={
            "status": "completed",
            "engineering_attempt": {
                "provider": "anthropic",
                "model": "claude-sonnet",
                "input_tokens": 12,
                "output_tokens": 5,
                "cost_microusd": 40_001,
                "cost_source": "provider_reported",
            },
        },
    )

    with pytest.raises(DBAPIError, match="append-only"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "UPDATE engineering_attempt_ledger SET project_id = NULL "
                    "WHERE idempotency_key = :key"
                ),
                {"key": f"engineering-run:{run_id}"},
            )

    deleted = await async_client.delete(f"/api/projects/{project['id']}")

    assert deleted.status_code == HTTPStatus.NO_CONTENT, deleted.text
    row = (
        await db_session.execute(
            text(
                "SELECT idempotency_key, run_id, project_id, story_id, task_id, user_id, "
                "input_tokens, output_tokens, cost_microusd, cost_source "
                "FROM engineering_attempt_ledger WHERE idempotency_key = :key"
            ),
            {"key": f"engineering-run:{run_id}"},
        )
    ).one()
    assert row == (
        f"engineering-run:{run_id}",
        None,
        None,
        None,
        None,
        owner["id"],
        12,
        5,
        40_001,
        "provider_reported",
    )

    with pytest.raises(DBAPIError, match="append-only"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "UPDATE engineering_attempt_ledger SET provider = 'rewritten' "
                    "WHERE idempotency_key = :key"
                ),
                {"key": f"engineering-run:{run_id}"},
            )

    balance = await async_client.get(f"/api/engineering-budget-policies/{owner['id']}/balance")
    assert balance.status_code == HTTPStatus.OK, balance.text
    assert balance.json() == {
        "user_id": owner["id"],
        "policy": None,
        "enforcement": "unlimited",
        "known_spend_microusd": 40_001,
        "active_held_microusd": 0,
        "unknown_final_held_microusd": 0,
        "available_microusd": None,
        "remaining_microusd": None,
        "exhausted": False,
        "unknown_cost_attempt_count": 0,
        "incomplete_coverage": False,
    }


@pytest.mark.asyncio
async def test_engineering_budget_policy_writes_are_versioned_and_idempotent(
    async_client: AsyncClient,
):
    user = await _user(async_client, uuid.uuid4().int % 1_000_000_000)

    created = await async_client.put(
        f"/api/engineering-budget-policies/{user['id']}",
        json={
            "limit_microusd": 100_000,
            "attempt_reservation_microusd": 10_000,
            "state": "enabled",
        },
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    assert created.json()["limit_microusd"] == 100_000
    assert created.json()["attempt_reservation_microusd"] == 10_000
    assert created.json()["state"] == "enabled"
    assert created.json()["version"] == 1

    non_integer = await async_client.put(
        f"/api/engineering-budget-policies/{user['id']}",
        json={
            "limit_microusd": 100_000.0,
            "attempt_reservation_microusd": 10_000,
            "state": "enabled",
        },
    )
    assert non_integer.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    same = await async_client.put(
        f"/api/engineering-budget-policies/{user['id']}",
        json={
            "limit_microusd": 100_000,
            "attempt_reservation_microusd": 10_000,
            "state": "enabled",
        },
    )
    assert same.status_code == HTTPStatus.OK
    assert same.json()["version"] == 1

    stale = await async_client.put(
        f"/api/engineering-budget-policies/{user['id']}",
        json={
            "limit_microusd": 50_000,
            "attempt_reservation_microusd": 10_000,
            "state": "enabled",
            "version": 2,
        },
    )
    assert stale.status_code == HTTPStatus.CONFLICT
    after_stale = await async_client.get(f"/api/engineering-budget-policies/{user['id']}")
    assert after_stale.json()["policy"]["limit_microusd"] == 100_000
    assert after_stale.json()["policy"]["version"] == 1

    disabled = await async_client.put(
        f"/api/engineering-budget-policies/{user['id']}",
        json={
            "limit_microusd": 100_000,
            "attempt_reservation_microusd": 10_000,
            "state": "disabled",
            "version": 1,
        },
    )
    assert disabled.status_code == HTTPStatus.OK
    assert disabled.json()["state"] == "disabled"
    assert disabled.json()["version"] == 2

    reenabled_zero = await async_client.put(
        f"/api/engineering-budget-policies/{user['id']}",
        json={
            "limit_microusd": 0,
            "attempt_reservation_microusd": 10_000,
            "state": "enabled",
            "version": 2,
        },
    )
    assert reenabled_zero.status_code == HTTPStatus.OK
    assert reenabled_zero.json()["state"] == "enabled"
    assert reenabled_zero.json()["version"] == 3


@pytest.mark.asyncio
async def test_engineering_budget_admission_reserves_before_handoff_and_is_idempotent(
    async_client: AsyncClient,
):
    """An enabled zero budget denies before any dispatch-side effect is possible."""
    user = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    await async_client.put(
        f"/api/engineering-budget-policies/{user['id']}",
        json={"limit_microusd": 0, "attempt_reservation_microusd": 1, "state": "enabled"},
    )
    project = await _project(async_client, user["telegram_id"])
    command = {
        "attempt_id": f"eng-budget-admission-{uuid.uuid4().hex}",
        "project_id": project["id"],
        "task_id": "budget-admission-task",
    }
    denied = await async_client.post("/api/engineering-budget-policies/admissions", json=command)
    assert denied.status_code == HTTPStatus.OK, denied.text
    assert denied.json()["outcome"] == "denied"

    repeated = await async_client.post("/api/engineering-budget-policies/admissions", json=command)
    assert repeated.status_code == HTTPStatus.OK, repeated.text
    assert repeated.json()["attempt_id"] == denied.json()["attempt_id"]
    assert repeated.json()["outcome"] == denied.json()["outcome"] == "denied"

    conflict = await async_client.post(
        "/api/engineering-budget-policies/admissions",
        json={**command, "task_id": "different-task"},
    )
    assert conflict.status_code == HTTPStatus.CONFLICT


@pytest.mark.asyncio
async def test_engineering_budget_holds_release_and_settle_without_double_counting(
    async_client: AsyncClient,
):
    user = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    project = await _project(async_client, user["telegram_id"])
    await async_client.put(
        f"/api/engineering-budget-policies/{user['id']}",
        json={
            "limit_microusd": 100,
            "attempt_reservation_microusd": 60,
            "state": "enabled",
        },
    )
    first_id = f"eng-budget-known-{uuid.uuid4().hex}"
    admitted = await async_client.post(
        "/api/engineering-budget-policies/admissions",
        json={"attempt_id": first_id, "project_id": project["id"], "task_id": "budget-known"},
    )
    assert admitted.status_code == HTTPStatus.OK, admitted.text
    assert admitted.json()["outcome"] == "admitted"

    balance = await async_client.get(f"/api/engineering-budget-policies/{user['id']}/balance")
    assert balance.json()["known_spend_microusd"] == 0
    assert balance.json()["active_held_microusd"] == 60
    assert balance.json()["remaining_microusd"] == 40

    second = await async_client.post(
        "/api/engineering-budget-policies/admissions",
        json={
            "attempt_id": f"eng-budget-denied-{uuid.uuid4().hex}",
            "project_id": project["id"],
            "task_id": "x",
        },
    )
    assert second.json()["outcome"] == "denied"
    released = await async_client.post(
        f"/api/engineering-budget-policies/admissions/{first_id}/release"
    )
    assert released.status_code == HTTPStatus.NO_CONTENT
    balance = await async_client.get(f"/api/engineering-budget-policies/{user['id']}/balance")
    assert balance.json()["active_held_microusd"] == 0

    settled_id = f"eng-budget-settled-{uuid.uuid4().hex}"
    settled = await async_client.post(
        "/api/engineering-budget-policies/admissions",
        json={"attempt_id": settled_id, "project_id": project["id"], "task_id": "budget-settled"},
    )
    assert settled.json()["outcome"] == "admitted"
    created = await async_client.post(
        "/api/runs/",
        json={"id": settled_id, "type": "engineering", "project_id": project["id"]},
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    terminal = await async_client.patch(
        f"/api/runs/{settled_id}",
        json={
            "status": "completed",
            "engineering_attempt": {
                "provider": "anthropic",
                "cost_microusd": 20,
                "cost_source": "provider_reported",
            },
        },
    )
    assert terminal.status_code == HTTPStatus.OK, terminal.text
    balance = await async_client.get(f"/api/engineering-budget-policies/{user['id']}/balance")
    assert balance.json()["known_spend_microusd"] == 20
    assert balance.json()["active_held_microusd"] == 0
    assert balance.json()["unknown_final_held_microusd"] == 0
    assert balance.json()["remaining_microusd"] == 80


@pytest.mark.asyncio
async def test_concurrent_boundary_admissions_have_one_winner(async_client: AsyncClient):
    """The policy-row lock serializes two different attempts at the boundary."""
    user = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    project = await _project(async_client, user["telegram_id"])
    await async_client.put(
        f"/api/engineering-budget-policies/{user['id']}",
        json={"limit_microusd": 100, "attempt_reservation_microusd": 100, "state": "enabled"},
    )

    async def admit(task_id: str) -> str:
        response = await async_client.post(
            "/api/engineering-budget-policies/admissions",
            json={
                "attempt_id": f"eng-boundary-{task_id}-{uuid.uuid4().hex}",
                "project_id": project["id"],
                "task_id": task_id,
            },
        )
        assert response.status_code == HTTPStatus.OK, response.text
        return response.json()["outcome"]

    outcomes = await asyncio.gather(admit("one"), admit("two"))

    assert sorted(outcomes) == ["admitted", "denied"]


@pytest.mark.asyncio
async def test_terminal_unknown_deploy_fix_retains_the_conservative_reservation(
    async_client: AsyncClient,
):
    user = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    project = await _project(async_client, user["telegram_id"])
    await async_client.put(
        f"/api/engineering-budget-policies/{user['id']}",
        json={"limit_microusd": 100, "attempt_reservation_microusd": 60, "state": "enabled"},
    )
    deploy_run_id = f"deploy-unknown-reservation-{uuid.uuid4().hex}"
    run_id = f"eng-deploy-fix-{deploy_run_id}-1"
    admitted = await async_client.post(
        "/api/engineering-budget-policies/admissions",
        json={"attempt_id": run_id, "project_id": project["id"], "task_id": run_id},
    )
    assert admitted.json()["outcome"] == "admitted"
    assert (
        await async_client.post(
            "/api/runs/", json={"id": run_id, "type": "engineering", "project_id": project["id"]}
        )
    ).status_code == HTTPStatus.CREATED
    assert (
        await async_client.patch(f"/api/runs/{run_id}", json={"status": "failed"})
    ).status_code == HTTPStatus.OK

    balance = await async_client.get(f"/api/engineering-budget-policies/{user['id']}/balance")
    assert balance.json()["known_spend_microusd"] == 0
    assert balance.json()["active_held_microusd"] == 0
    assert balance.json()["unknown_final_held_microusd"] == 60


@pytest.mark.asyncio
async def test_manual_invalid_task_is_refused_before_budget_admission(
    async_client: AsyncClient, db_session
):
    """A cheap typed refusal cannot create a hold with no Run."""
    user = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    project = await _project(async_client, user["telegram_id"])
    await async_client.put(
        f"/api/engineering-budget-policies/{user['id']}",
        json={"limit_microusd": 100, "attempt_reservation_microusd": 60, "state": "enabled"},
    )
    created = await async_client.post(
        "/api/tasks/",
        json={"project_id": project["id"], "title": "Already done", "type": "feature"},
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    task = await db_session.get(Task, created.json()["id"])
    assert task is not None
    task.status = "done"
    await db_session.commit()

    refused = await async_client.post(f"/api/tasks/{task.id}/spawn-worker", json={"actor": "test"})
    assert refused.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    reservations = (
        (
            await db_session.execute(
                select(EngineeringBudgetReservation).where(
                    EngineeringBudgetReservation.task_id == task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert reservations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["recipient", "publish"])
async def test_manual_pre_handoff_failures_release_the_admitted_reservation(
    async_client: AsyncClient, db_session, monkeypatch, failure: str
):
    """Manual recipient and queue failures prove no provider work could have started."""
    from src.routers import _task_actions

    user = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    project = await _project(async_client, user["telegram_id"])
    await async_client.put(
        f"/api/engineering-budget-policies/{user['id']}",
        json={"limit_microusd": 100, "attempt_reservation_microusd": 60, "state": "enabled"},
    )
    created = await async_client.post(
        "/api/tasks/",
        json={"project_id": project["id"], "title": "Dispatch failure", "type": "feature"},
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    if failure == "recipient":
        monkeypatch.setattr(
            _task_actions,
            "resolve_project_chat_id",
            AsyncMock(side_effect=RuntimeError("recipient unavailable")),
        )
    else:
        monkeypatch.setattr(
            RedisStreamClient,
            "publish_message",
            AsyncMock(side_effect=RuntimeError("redis unavailable")),
        )

    refused = await async_client.post(
        f"/api/tasks/{created.json()['id']}/spawn-worker", json={"actor": "test"}
    )
    assert refused.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    reservation = await db_session.scalar(
        select(EngineeringBudgetReservation).where(
            EngineeringBudgetReservation.task_id == created.json()["id"]
        )
    )
    assert reservation is not None
    assert reservation.state == "released"
    assert reservation.active_held_microusd == 0


@pytest.mark.asyncio
async def test_engineering_budget_balance_uses_ledger_user_id_and_marks_unknown_coverage(
    async_client: AsyncClient, db_session
):
    from datetime import UTC, datetime

    user = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    other = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    now = datetime.now(UTC)
    db_session.add_all(
        [
            EngineeringAttemptLedger(
                idempotency_key=f"known-{uuid.uuid4().hex}",
                user_id=user["id"],
                owner_attribution="resolved",
                role="engineering",
                occurred_at=now,
                provider="anthropic",
                cost_microusd=40_001,
                cost_source="provider_reported",
            ),
            EngineeringAttemptLedger(
                idempotency_key=f"unknown-{uuid.uuid4().hex}",
                user_id=user["id"],
                owner_attribution="resolved",
                role="engineering",
                occurred_at=now,
                cost_source="unknown",
            ),
            EngineeringAttemptLedger(
                idempotency_key=f"other-{uuid.uuid4().hex}",
                user_id=other["id"],
                owner_attribution="resolved",
                role="engineering",
                occurred_at=now,
                provider="anthropic",
                cost_microusd=999_999,
                cost_source="provider_reported",
            ),
        ]
    )
    await db_session.commit()

    await async_client.put(
        f"/api/engineering-budget-policies/{user['id']}",
        json={"limit_microusd": 40_001, "attempt_reservation_microusd": 10_000, "state": "enabled"},
    )
    balance = await async_client.get(f"/api/engineering-budget-policies/{user['id']}/balance")
    assert balance.status_code == HTTPStatus.OK, balance.text
    assert balance.json() == {
        "user_id": user["id"],
        "policy": {
            "user_id": user["id"],
            "limit_microusd": 40_001,
            "attempt_reservation_microusd": 10_000,
            "state": "enabled",
            "version": 1,
        },
        "enforcement": "enforced",
        "known_spend_microusd": 40_001,
        "active_held_microusd": 0,
        "unknown_final_held_microusd": 0,
        "available_microusd": 0,
        "remaining_microusd": 0,
        "exhausted": True,
        "unknown_cost_attempt_count": 1,
        "incomplete_coverage": True,
    }


@pytest.mark.asyncio
async def test_engineering_budget_policy_self_read_and_cross_user_refusal(
    async_client: AsyncClient,
):
    owner = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    intruder = await _user(async_client, uuid.uuid4().int % 1_000_000_000)
    await async_client.put(
        f"/api/engineering-budget-policies/{owner['id']}",
        json={"limit_microusd": 100, "attempt_reservation_microusd": 10, "state": "enabled"},
    )

    own = await async_client.get(
        "/api/engineering-budget-policy/balance",
        headers={"X-Telegram-ID": str(owner["telegram_id"])},
    )
    assert own.status_code == HTTPStatus.OK
    assert own.json()["user_id"] == owner["id"]

    own_policy = await async_client.get(
        "/api/engineering-budget-policy",
        headers={"X-Telegram-ID": str(owner["telegram_id"])},
    )
    assert own_policy.status_code == HTTPStatus.OK
    assert own_policy.json()["policy"]["user_id"] == owner["id"]

    admin = await _user(async_client, uuid.uuid4().int % 1_000_000_000, is_admin=True)
    admin_write = await async_client.put(
        f"/api/engineering-budget-policies/{owner['id']}",
        json={
            "limit_microusd": 101,
            "attempt_reservation_microusd": 10,
            "state": "enabled",
            "version": 1,
        },
        headers={"X-Telegram-ID": str(admin["telegram_id"])},
    )
    assert admin_write.status_code == HTTPStatus.OK
    assert admin_write.json()["version"] == 2

    for path in (
        f"/api/engineering-budget-policies/{owner['id']}",
        f"/api/engineering-budget-policies/{owner['id']}/balance",
    ):
        refused = await async_client.get(
            path, headers={"X-Telegram-ID": str(intruder["telegram_id"])}
        )
        assert refused.status_code == HTTPStatus.FORBIDDEN

    write_refused = await async_client.put(
        f"/api/engineering-budget-policies/{owner['id']}",
        json={
            "limit_microusd": 0,
            "attempt_reservation_microusd": 10,
            "state": "disabled",
            "version": 2,
        },
        headers={"X-Telegram-ID": str(owner["telegram_id"])},
    )
    assert write_refused.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.asyncio
async def test_migration_backfills_only_terminal_runs_and_enforces_constraints(db_session):
    """Execute the revision against an isolated PostgreSQL schema with historical rows."""
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    schema = f"ledger_migration_{uuid.uuid4().hex}"
    migration_path = (
        Path(__file__).parents[2]
        / "migrations/versions/8d2c5e6f7a8b_add_engineering_attempt_ledger.py"
    )
    spec = importlib.util.spec_from_file_location("ledger_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run_migration(session):
        connection = session.connection()
        quoted_schema = f'"{schema}"'
        connection.execute(text(f"CREATE SCHEMA {quoted_schema}"))
        connection.execute(text(f"SET LOCAL search_path TO {quoted_schema}, public"))
        for sql in (
            "CREATE TABLE users (id integer PRIMARY KEY)",
            "CREATE TABLE projects (id uuid PRIMARY KEY, owner_id integer)",
            "CREATE TABLE stories (id varchar(255) PRIMARY KEY)",
            "CREATE TABLE tasks (id varchar(255) PRIMARY KEY)",
            """CREATE TABLE runs (
                id varchar(255) PRIMARY KEY, type varchar(50) NOT NULL,
                status varchar(50) NOT NULL, project_id uuid, story_id varchar(255),
                task_id varchar(255), created_at timestamptz NOT NULL,
                started_at timestamptz, completed_at timestamptz, agent_profile jsonb,
                input_tokens integer, output_tokens integer, total_tokens integer
            )""",
            "INSERT INTO users VALUES (1)",
            "INSERT INTO projects VALUES ('00000000-0000-0000-0000-000000000001', 1)",
            """INSERT INTO runs (id, type, status, project_id, created_at) VALUES
                ('terminal-completed', 'engineering', 'completed',
                 '00000000-0000-0000-0000-000000000001', now()),
                ('terminal-failed', 'engineering', 'failed',
                 '00000000-0000-0000-0000-000000000001', now()),
                ('terminal-cancelled', 'engineering', 'cancelled',
                 '00000000-0000-0000-0000-000000000001', now()),
                ('queued', 'engineering', 'queued',
                 '00000000-0000-0000-0000-000000000001', now()),
                ('running', 'engineering', 'running',
                 '00000000-0000-0000-0000-000000000001', now()),
                ('other', 'deploy', 'completed',
                 '00000000-0000-0000-0000-000000000001', now())""",
        ):
            connection.execute(text(sql))
        original_op = migration.op
        migration.op = Operations(MigrationContext.configure(connection))
        try:
            migration.upgrade()
            rows = (
                connection.execute(
                    text("SELECT run_id FROM engineering_attempt_ledger ORDER BY run_id")
                )
                .scalars()
                .all()
            )
            assert rows == ["terminal-cancelled", "terminal-completed", "terminal-failed"]
            for sql in (
                """INSERT INTO engineering_attempt_ledger
                   (id, idempotency_key, run_id, owner_attribution, role, occurred_at, cost_source)
                   VALUES (uuid_generate_v4(), 'engineering-run:terminal-completed-duplicate',
                   'terminal-completed', 'unknown', 'engineering', now(), 'unknown')""",
                """INSERT INTO engineering_attempt_ledger
                   (id, idempotency_key, run_id, owner_attribution, role, occurred_at,
                    cost_microusd, cost_source)
                   VALUES (uuid_generate_v4(), 'engineering-run:queued', 'queued', 'unknown',
                   'engineering', now(), 1, 'unknown')""",
                """INSERT INTO engineering_attempt_ledger
                   (id, idempotency_key, run_id, owner_attribution, role, occurred_at,
                    cost_microusd, cost_source)
                   VALUES (uuid_generate_v4(), 'engineering-run:running', 'running', 'unknown',
                   'engineering', now(), 1, 'provider_reported')""",
            ):
                try:
                    with connection.begin_nested():
                        connection.execute(text(sql))
                except IntegrityError:
                    continue
                raise AssertionError(f"database accepted invalid ledger row: {sql}")

            for sql in (
                "UPDATE engineering_attempt_ledger SET model = 'rewritten' "
                "WHERE run_id = 'terminal-completed'",
                "DELETE FROM engineering_attempt_ledger WHERE run_id = 'terminal-completed'",
            ):
                with pytest.raises(DBAPIError, match="append-only"):
                    with connection.begin_nested():
                        connection.execute(text(sql))
        finally:
            migration.op = original_op
            connection.execute(text(f"DROP SCHEMA {quoted_schema} CASCADE"))

    await db_session.run_sync(run_migration)


@pytest.mark.asyncio
async def test_engineering_budget_policy_migration_enforces_row_constraints(db_session):
    """The durable lock row cannot hold negative money or a non-version."""
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    schema = f"budget_policy_migration_{uuid.uuid4().hex}"
    migration_path = (
        Path(__file__).parents[2]
        / "migrations/versions/f5e2d3c4b1a0_add_engineering_budget_policies.py"
    )
    spec = importlib.util.spec_from_file_location("budget_policy_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run_migration(session):
        connection = session.connection()
        quoted_schema = f'"{schema}"'
        connection.execute(text(f"CREATE SCHEMA {quoted_schema}"))
        connection.execute(text(f"SET LOCAL search_path TO {quoted_schema}, public"))
        connection.execute(text("CREATE TABLE users (id integer PRIMARY KEY)"))
        connection.execute(text("INSERT INTO users VALUES (1), (2)"))
        original_op = migration.op
        migration.op = Operations(MigrationContext.configure(connection))
        try:
            migration.upgrade()
            connection.execute(
                text(
                    "INSERT INTO engineering_budget_policies "
                    "(user_id, limit_microusd, state, version) VALUES (1, 0, 'enabled', 1)"
                )
            )
            for sql in (
                "INSERT INTO engineering_budget_policies "
                "(user_id, limit_microusd, state, version) VALUES (2, -1, 'enabled', 1)",
                "UPDATE engineering_budget_policies SET version = 0 WHERE user_id = 1",
            ):
                with pytest.raises(IntegrityError):
                    with connection.begin_nested():
                        connection.execute(text(sql))
        finally:
            migration.op = original_op
            connection.execute(text(f"DROP SCHEMA {quoted_schema} CASCADE"))

    await db_session.run_sync(run_migration)


@pytest.mark.asyncio
async def test_engineering_budget_reservation_migration_enforces_authoritative_indexes(
    db_session,
):
    """Reservation identity, non-negative holds and model-aligned indexes are durable."""
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    schema = f"budget_reservation_migration_{uuid.uuid4().hex}"
    migration_path = (
        Path(__file__).parents[2]
        / "migrations/versions/a6b7c8d9e0f1_add_engineering_budget_reservations.py"
    )
    spec = importlib.util.spec_from_file_location("budget_reservation_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run_migration(session):
        connection = session.connection()
        quoted_schema = f'"{schema}"'
        connection.execute(text(f"CREATE SCHEMA {quoted_schema}"))
        connection.execute(text(f"SET LOCAL search_path TO {quoted_schema}, public"))
        connection.execute(text("CREATE TABLE users (id integer PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE projects (id uuid PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE engineering_budget_policies ("
                "user_id integer PRIMARY KEY, limit_microusd bigint NOT NULL, "
                "state varchar NOT NULL, version integer NOT NULL)"
            )
        )
        original_op = migration.op
        migration.op = Operations(MigrationContext.configure(connection))
        try:
            migration.upgrade()
            connection.execute(text("INSERT INTO users VALUES (1), (2)"))
            connection.execute(
                text(
                    "INSERT INTO engineering_budget_reservations "
                    "(id, idempotency_key, attempt_id, user_id, outcome, reservation_microusd, "
                    "known_spend_microusd, active_held_microusd) "
                    "VALUES ('00000000-0000-0000-0000-000000000001', 'engineering-run:one', "
                    "'eng-one', 1, "
                    "'admitted', 1, 0, 1)"
                )
            )
            for sql in (
                "INSERT INTO engineering_budget_reservations "
                "(id, idempotency_key, attempt_id, user_id, outcome, reservation_microusd, "
                "known_spend_microusd, active_held_microusd) "
                "VALUES ('00000000-0000-0000-0000-000000000002', 'engineering-run:two', "
                "'eng-two', 2, "
                "'admitted', -1, 0, 0)",
                "INSERT INTO engineering_budget_reservations "
                "(id, idempotency_key, attempt_id, user_id, outcome, reservation_microusd, "
                "known_spend_microusd, active_held_microusd) "
                "VALUES ('00000000-0000-0000-0000-000000000003', 'engineering-run:one', "
                "'eng-three', 2, "
                "'admitted', 1, 0, 1)",
            ):
                with pytest.raises(IntegrityError):
                    with connection.begin_nested():
                        connection.execute(text(sql))
            indexes = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = current_schema() "
                        "AND tablename = 'engineering_budget_reservations'"
                    )
                )
            }
            assert {
                "ix_engineering_budget_reservations_user_id",
                "ix_engineering_budget_reservations_project_id",
                "ix_engineering_budget_reservations_story_id",
                "ix_engineering_budget_reservations_task_id",
                "ix_engineering_budget_reservation_user_state",
            } <= indexes
        finally:
            migration.op = original_op
            connection.execute(text(f"DROP SCHEMA {quoted_schema} CASCADE"))

    await db_session.run_sync(run_migration)
