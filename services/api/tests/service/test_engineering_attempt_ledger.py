"""Service coverage for canonical engineering-attempt accounting."""

from http import HTTPStatus
import uuid

from httpx import AsyncClient
import pytest


async def _user(client: AsyncClient, telegram_id: int) -> dict:
    response = await client.post(
        "/api/users/",
        json={"telegram_id": telegram_id, "username": f"ledger_{telegram_id}"},
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
