"""Service coverage for promo-gated registration."""

import asyncio
from http import HTTPStatus
import uuid

import pytest


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
    )
    assert reused.status_code == HTTPStatus.CONFLICT
    assert reused.json()["detail"]["code"] == "promo_code_redeemed"

    topped_up = await async_client.post(
        "/api/users/upsert",
        json={"telegram_id": 810_000_001, "promo_code": second_code},
    )
    assert topped_up.status_code == HTTPStatus.CONFLICT
    assert topped_up.json()["detail"]["code"] == "user_already_has_policy"

    codes = await async_client.get("/api/promo-codes")
    assert codes.status_code == HTTPStatus.OK
    second = next(item for item in codes.json() if item["code"] == second_code)
    assert second["redeemed_by_user_id"] is None


@pytest.mark.asyncio
async def test_concurrent_redemption_has_one_winner(async_client) -> None:
    minted = await async_client.post(
        "/api/promo-codes/batch",
        json={"quantity": 1, "credits_microusd": 1_000_000, "attempt_reservation_microusd": 1},
    )
    code = minted.json()[0]["code"]

    first, second = await asyncio.gather(
        async_client.post(
            "/api/users/upsert", json={"telegram_id": 810_000_011, "promo_code": code}
        ),
        async_client.post(
            "/api/users/upsert", json={"telegram_id": 810_000_012, "promo_code": code}
        ),
    )

    responses = [first, second]
    assert sum(response.status_code == HTTPStatus.OK for response in responses) == 1
    loser = next(response for response in responses if response.status_code != HTTPStatus.OK)
    assert loser.status_code == HTTPStatus.CONFLICT
    assert loser.json()["detail"]["code"] == "promo_code_redeemed"


@pytest.mark.asyncio
async def test_unknown_cost_keeps_promo_reservation_held(async_client) -> None:
    minted = await async_client.post(
        "/api/promo-codes/batch",
        json={"quantity": 1, "credits_microusd": 10, "attempt_reservation_microusd": 10},
    )
    activated = await async_client.post(
        "/api/users/upsert",
        json={"telegram_id": 810_000_021, "promo_code": minted.json()[0]["code"]},
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
