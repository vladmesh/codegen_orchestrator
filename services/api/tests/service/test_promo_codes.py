"""Service coverage for promo-gated registration."""

import asyncio
from http import HTTPStatus
import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select, text

from shared.models import User
from src.dependencies import create_lk_jwt


@pytest.mark.asyncio
async def test_promo_activation_arms_policy_and_cannot_be_reused(async_client) -> None:
    minted = await async_client.post(
        "/api/promo-codes/batch",
        json={
            "quantity": 2,
            "credits_microusd": 1_000_000,
            "attempt_reservation_microusd": 200_000,
        },
    )
    assert minted.status_code == HTTPStatus.CREATED, minted.text
    first_code, second_code = (item["code"] for item in minted.json())

    required = await async_client.post(
        "/api/users/upsert",
        json={"telegram_id": 810_000_001, "first_name": "Missing"},
        headers={"X-Telegram-ID": "810000001"},
    )
    assert required.status_code == HTTPStatus.FORBIDDEN
    assert required.json()["detail"]["code"] == "promo_code_required"

    activated = await async_client.post(
        "/api/users/upsert",
        json={
            "telegram_id": 810_000_001,
            "first_name": "Activated",
            "promo_code": first_code.lower(),
        },
        headers={"X-Telegram-ID": "810000001"},
    )
    assert activated.status_code == HTTPStatus.OK, activated.text
    user_id = activated.json()["id"]

    policy = await async_client.get(f"/api/engineering-budget-policies/{user_id}")
    assert policy.status_code == HTTPStatus.OK, policy.text
    assert policy.json()["policy"] == {
        "user_id": user_id,
        "limit_microusd": 1_000_000,
        "attempt_reservation_microusd": 200_000,
        "state": "enabled",
        "version": 1,
    }

    reused = await async_client.post(
        "/api/users/",
        json={"telegram_id": 810_000_002, "promo_code": first_code},
        headers={"X-Telegram-ID": "810000002"},
    )
    assert reused.status_code == HTTPStatus.CONFLICT
    assert reused.json()["detail"]["code"] == "promo_code_redeemed"

    topped_up = await async_client.post(
        "/api/users/upsert",
        json={"telegram_id": 810_000_001, "promo_code": second_code},
        headers={"X-Telegram-ID": "810000001"},
    )
    assert topped_up.status_code == HTTPStatus.CONFLICT
    assert topped_up.json()["detail"]["code"] == "user_already_has_policy"

    codes = await async_client.get("/api/promo-codes")
    assert codes.status_code == HTTPStatus.OK
    second = next(item for item in codes.json() if item["code"] == second_code)
    assert second["redeemed_by_user_id"] is None


@pytest.mark.asyncio
async def test_service_created_user_is_not_admin_without_an_admin_grant(async_client) -> None:
    created = await async_client.post(
        "/api/users/",
        json={"telegram_id": 810_000_003, "first_name": "Service user"},
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    assert created.json()["is_admin"] is False


@pytest.mark.asyncio
async def test_lk_bearer_cannot_impersonate_for_promo_or_policy(async_client) -> None:
    ordinary = await async_client.post("/api/users/", json={"telegram_id": 810_000_031})
    target = await async_client.post(
        "/api/users/", json={"telegram_id": 810_000_032, "is_admin": True}
    )
    token = create_lk_jwt(ordinary.json()["id"])
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Telegram-ID": str(target.json()["telegram_id"]),
    }
    from src.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as bearer_client:
        own = await bearer_client.get(
            "/api/engineering-budget-policy",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Telegram-ID": str(ordinary.json()["telegram_id"]),
            },
        )
        assert own.status_code == HTTPStatus.OK
        own_balance = await bearer_client.get(
            "/api/engineering-budget-policy/balance",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert own_balance.status_code == HTTPStatus.OK
        minted = await bearer_client.post(
            "/api/promo-codes/batch",
            json={"quantity": 1, "credits_microusd": 1, "attempt_reservation_microusd": 1},
            headers=headers,
        )
        assert minted.status_code == HTTPStatus.FORBIDDEN
        policy = await bearer_client.put(
            f"/api/engineering-budget-policies/{target.json()['id']}",
            json={"limit_microusd": 1, "attempt_reservation_microusd": 1, "state": "enabled"},
            headers=headers,
        )
        assert policy.status_code == HTTPStatus.FORBIDDEN
        foreign_policy = await bearer_client.get("/api/engineering-budget-policy", headers=headers)
        foreign_balance = await bearer_client.get(
            "/api/engineering-budget-policy/balance", headers=headers
        )
        assert foreign_policy.status_code == HTTPStatus.FORBIDDEN
        assert foreign_balance.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.asyncio
async def test_rag_unknown_telegram_user_is_not_registered(async_client, db_session) -> None:
    telegram_id = 810_000_099
    response = await async_client.post(
        "/api/rag/messages",
        json={"telegram_id": telegram_id, "role": "user", "message_text": "blocked"},
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert await db_session.scalar(select(User).where(User.telegram_id == telegram_id)) is None


@pytest.mark.asyncio
async def test_promo_code_migration_upgrades_and_downgrades(db_session) -> None:
    migration_path = (
        Path(__file__).parents[2] / "migrations/versions/f6e7d8c9b0a1_add_promo_codes.py"
    )
    spec = importlib.util.spec_from_file_location("promo_code_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run_migration(session):
        connection = session.connection()
        schema_name = f"promo_migration_{uuid.uuid4().hex}"
        schema = f'"{schema_name}"'
        connection.execute(text(f"CREATE SCHEMA {schema}"))
        connection.execute(text(f"SET LOCAL search_path TO {schema}, public"))
        connection.execute(text("CREATE TABLE users (id integer PRIMARY KEY)"))
        original_op = migration.op
        migration.op = Operations(MigrationContext.configure(connection))
        try:
            migration.upgrade()
            assert (
                connection.execute(
                    text(f"SELECT to_regclass('{schema_name}.promo_codes')")
                ).scalar()
                == "promo_codes"
            )
            migration.downgrade()
            assert (
                connection.execute(
                    text(f"SELECT to_regclass('{schema_name}.promo_codes')")
                ).scalar()
                is None
            )
        finally:
            migration.op = original_op
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))

    await db_session.run_sync(run_migration)


@pytest.mark.asyncio
async def test_concurrent_redemption_has_one_winner(async_client) -> None:
    minted = await async_client.post(
        "/api/promo-codes/batch",
        json={"quantity": 1, "credits_microusd": 1_000_000, "attempt_reservation_microusd": 1},
    )
    code = minted.json()[0]["code"]

    first, second = await asyncio.gather(
        async_client.post(
            "/api/users/upsert",
            json={"telegram_id": 810_000_011, "promo_code": code},
            headers={"X-Telegram-ID": "810000011"},
        ),
        async_client.post(
            "/api/users/upsert",
            json={"telegram_id": 810_000_012, "promo_code": code},
            headers={"X-Telegram-ID": "810000012"},
        ),
    )

    responses = [first, second]
    assert sum(response.status_code == HTTPStatus.OK for response in responses) == 1
    loser = next(response for response in responses if response.status_code != HTTPStatus.OK)
    assert loser.status_code == HTTPStatus.CONFLICT
    assert loser.json()["detail"]["code"] == "promo_code_redeemed"


@pytest.mark.asyncio
async def test_concurrent_different_codes_for_one_user_are_typed(async_client) -> None:
    minted = await async_client.post(
        "/api/promo-codes/batch",
        json={"quantity": 2, "credits_microusd": 1, "attempt_reservation_microusd": 1},
    )
    first_code, second_code = (item["code"] for item in minted.json())
    headers = {"X-Telegram-ID": "810000013"}
    first, second = await asyncio.gather(
        async_client.post(
            "/api/users/upsert",
            json={"telegram_id": 810_000_013, "promo_code": first_code},
            headers=headers,
        ),
        async_client.post(
            "/api/users/upsert",
            json={"telegram_id": 810_000_013, "promo_code": second_code},
            headers=headers,
        ),
    )
    assert sum(response.status_code == HTTPStatus.OK for response in (first, second)) == 1
    loser = next(response for response in (first, second) if response.status_code != HTTPStatus.OK)
    assert loser.status_code == HTTPStatus.CONFLICT
    assert loser.json()["detail"]["code"] == "user_already_registered"


@pytest.mark.asyncio
async def test_unknown_cost_keeps_promo_reservation_held(async_client) -> None:
    minted = await async_client.post(
        "/api/promo-codes/batch",
        json={"quantity": 1, "credits_microusd": 10, "attempt_reservation_microusd": 10},
    )
    activated = await async_client.post(
        "/api/users/upsert",
        json={"telegram_id": 810_000_021, "promo_code": minted.json()[0]["code"]},
        headers={"X-Telegram-ID": "810000021"},
    )
    user = activated.json()
    project = await async_client.post(
        "/api/projects/",
        json={
            "id": str(uuid.uuid4()),
            "title": "Promo hold",
            "initiating_run_id": f"init-{uuid.uuid4().hex}",
            "config": {},
        },
        headers={"X-Telegram-ID": str(user["telegram_id"])},
    )
    attempt_id = f"promo-unknown-{uuid.uuid4().hex}"
    admission = await async_client.post(
        "/api/engineering-budget-policies/admissions",
        json={"attempt_id": attempt_id, "project_id": project.json()["id"], "task_id": attempt_id},
    )
    assert admission.json()["reservation_state"] == "active"
    assert (
        await async_client.post(
            "/api/runs/",
            json={"id": attempt_id, "type": "engineering", "project_id": project.json()["id"]},
        )
    ).status_code == HTTPStatus.CREATED
    assert (
        await async_client.patch(
            f"/api/runs/{attempt_id}",
            json={"status": "failed", "engineering_attempt": {"cost_source": "unknown"}},
        )
    ).status_code == HTTPStatus.OK
    balance = await async_client.get(f"/api/engineering-budget-policies/{user['id']}/balance")
    assert balance.json()["unknown_final_held_microusd"] == 10
    assert balance.json()["exhausted"] is True
