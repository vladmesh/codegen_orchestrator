"""Service coverage for promo-gated registration."""

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
