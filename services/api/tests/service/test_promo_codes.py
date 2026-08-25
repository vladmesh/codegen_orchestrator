"""Service coverage for promo-gated registration."""

import asyncio
from http import HTTPStatus

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
