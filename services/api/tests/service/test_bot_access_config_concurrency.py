"""Concurrent bot-audience and generic config writes retain both updates."""

import asyncio
import uuid

from fastapi import status
from httpx import AsyncClient
import pytest


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
