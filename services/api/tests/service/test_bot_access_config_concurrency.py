"""Concurrent bot-audience and generic config writes retain both updates."""

import asyncio
import uuid

from fastapi import status
from httpx import AsyncClient
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "audience"),
    [
        ("only_me", "42"),
        ("public", ""),
        ("invite", "42"),
        ("custom", "42,84"),
    ],
)
async def test_bot_access_endpoint_records_each_contract_audience(
    async_client: AsyncClient, mode: str, audience: str
):
    """The PO's four choices persist their exact template contract literal."""
    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = uuid.uuid4()

    user = await async_client.post(
        "/api/users/",
        json={"telegram_id": telegram_id, "username": f"bot_access_{telegram_id}"},
    )
    assert user.status_code == status.HTTP_201_CREATED

    created = await async_client.post(
        "/api/projects/",
        json={"id": str(project_id), "title": "Bot Access", "config": {}},
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert created.status_code == status.HTTP_201_CREATED

    response = await async_client.post(
        f"/api/projects/{project_id}/config/bot-access",
        json={"mode": mode, "allowed_telegram_ids": audience},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"mode": mode, "allowed_telegram_ids": audience}
    project = await async_client.get(f"/api/projects/{project_id}")
    assert project.status_code == status.HTTP_200_OK
    assert project.json()["config"] == {
        "bot_access": {"mode": mode, "allowed_telegram_ids": audience},
        "env_overrides": {"TG_BOT_ALLOWED_TELEGRAM_IDS": audience},
    }


@pytest.mark.asyncio
async def test_bot_access_and_generic_config_update_do_not_lose_the_audience(
    async_client: AsyncClient,
):
    """Interleave both config writers; their shared row lock serializes the merge."""
    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = uuid.uuid4()

    user = await async_client.post(
        "/api/users/",
        json={"telegram_id": telegram_id, "username": f"bot_access_{telegram_id}"},
    )
    assert user.status_code == status.HTTP_201_CREATED

    created = await async_client.post(
        "/api/projects/",
        json={"id": str(project_id), "title": "Concurrent Bot Access", "config": {}},
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert created.status_code == status.HTTP_201_CREATED

    audience_write, config_write = await asyncio.gather(
        async_client.post(
            f"/api/projects/{project_id}/config/bot-access",
            json={"mode": "only_me", "allowed_telegram_ids": str(telegram_id)},
        ),
        async_client.patch(f"/api/projects/{project_id}", json={"config": {"tree": "src/"}}),
    )
    assert audience_write.status_code == status.HTTP_200_OK
    assert config_write.status_code == status.HTTP_200_OK

    project = await async_client.get(f"/api/projects/{project_id}")
    assert project.status_code == status.HTTP_200_OK
    assert project.json()["config"] == {
        "tree": "src/",
        "bot_access": {"mode": "only_me", "allowed_telegram_ids": str(telegram_id)},
        "env_overrides": {"TG_BOT_ALLOWED_TELEGRAM_IDS": str(telegram_id)},
    }
