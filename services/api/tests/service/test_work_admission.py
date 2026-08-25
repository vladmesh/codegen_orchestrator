"""Service-level proofs for atomic count-based work admission."""

import asyncio
from http import HTTPStatus
import uuid

from httpx import AsyncClient
import pytest


async def _set_config(client: AsyncClient, key: str, value: int | bool) -> None:
    response = await client.post(
        "/api/system-configs/",
        json={
            "key": key,
            "value": value,
            "category": "work_admission",
            "description": "Service test override",
            "updated_by": "test",
        },
    )
    assert response.status_code == HTTPStatus.CREATED, response.text


@pytest.mark.asyncio
async def test_concurrent_project_creations_cannot_pass_the_same_user_ceiling(
    async_client: AsyncClient,
):
    """The per-user row lock admits only one request when one slot remains."""
    telegram_id = 830_000_001
    created = await async_client.post(
        "/api/users/", json={"telegram_id": telegram_id, "username": "admission-race"}
    )
    assert created.status_code == HTTPStatus.CREATED, created.text

    await _set_config(async_client, "work_admission.max_projects_per_user", 1)
    try:

        async def create_project() -> int:
            response = await async_client.post(
                "/api/projects/",
                headers={"X-Telegram-ID": str(telegram_id)},
                json={
                    "id": str(uuid.uuid4()),
                    "title": "Concurrent admission",
                    "initiating_run_id": str(uuid.uuid4()),
                    "status": "active",
                    "config": {},
                },
            )
            if response.status_code == HTTPStatus.CONFLICT:
                assert response.json()["detail"]["admission"] == {
                    "outcome": "denied",
                    "reason": "project_limit",
                    "retryable": False,
                }
            return response.status_code

        statuses = await asyncio.gather(create_project(), create_project())
        assert sorted(statuses) == [HTTPStatus.CREATED, HTTPStatus.CONFLICT]
    finally:
        await _set_config(async_client, "work_admission.max_projects_per_user", 10000)
