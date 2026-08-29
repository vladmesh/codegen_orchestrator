"""Real-API regressions for the verified Telegram audience barrier."""

from __future__ import annotations

import uuid

from fastapi import status
from httpx import AsyncClient
import pytest
from sqlalchemy import select

from shared.models import Project, User

SENDER_TELEGRAM_ID = 5841442582
DICTATED_TELEGRAM_ID = 625038902


async def _create_user(async_client: AsyncClient, telegram_id: int) -> None:
    response = await async_client.post(
        "/api/users/",
        json={"telegram_id": telegram_id, "username": f"telegram_barrier_{telegram_id}"},
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text


@pytest.mark.asyncio
async def test_custom_audience_keeps_the_verified_sender_when_the_dialogue_dictates_an_id(
    async_client: AsyncClient, db_session
):
    """ "я, id625038902" means sender plus the dictated user, never a replacement."""
    sender_telegram_id = SENDER_TELEGRAM_ID
    await _create_user(async_client, sender_telegram_id)
    project_id = uuid.uuid4()

    created = await async_client.post(
        "/api/projects/",
        json={
            "id": str(project_id),
            "initiating_run_id": "telegram-id-barrier-run",
            "title": "Dictated audience",
            "config": {},
        },
        headers={"X-Telegram-ID": str(sender_telegram_id)},
    )
    assert created.status_code == status.HTTP_201_CREATED, created.text

    selected = await async_client.post(
        f"/api/projects/{project_id}/config/bot-access",
        json={"mode": "custom", "allowed_telegram_ids": str(DICTATED_TELEGRAM_ID)},
        headers={"X-Telegram-ID": str(sender_telegram_id)},
    )

    assert selected.status_code == status.HTTP_200_OK, selected.text
    assert selected.json()["allowed_telegram_ids"] == (
        f"{DICTATED_TELEGRAM_ID},{SENDER_TELEGRAM_ID}"
    )
    project = (
        await db_session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one()
    owner = await db_session.get(User, project.owner_id)
    assert owner is not None
    assert owner.telegram_id == SENDER_TELEGRAM_ID
    assert project.config["bot_access"]["allowed_telegram_ids"] == (
        f"{DICTATED_TELEGRAM_ID},{SENDER_TELEGRAM_ID}"
    )


@pytest.mark.asyncio
async def test_private_audience_cannot_exclude_its_owner_without_the_explicit_opt_out(
    async_client: AsyncClient,
):
    sender_telegram_id = SENDER_TELEGRAM_ID + 1
    await _create_user(async_client, sender_telegram_id)
    project_id = uuid.uuid4()
    created = await async_client.post(
        "/api/projects/",
        json={
            "id": str(project_id),
            "initiating_run_id": "telegram-id-barrier-owner-run",
            "title": "Owner barrier",
            "config": {},
        },
        headers={"X-Telegram-ID": str(sender_telegram_id)},
    )
    assert created.status_code == status.HTTP_201_CREATED, created.text

    refused = await async_client.post(
        f"/api/projects/{project_id}/config/bot-access",
        json={"mode": "only_me", "allowed_telegram_ids": str(DICTATED_TELEGRAM_ID)},
        headers={"X-Telegram-ID": str(sender_telegram_id)},
    )

    assert refused.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert "owner" in refused.json()["detail"]
    assert "allow_ownerless_audience" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_only_an_internal_service_may_record_an_ownerless_private_audience(
    async_client: AsyncClient,
):
    sender_telegram_id = SENDER_TELEGRAM_ID + 2
    await _create_user(async_client, sender_telegram_id)
    project_id = uuid.uuid4()
    created = await async_client.post(
        "/api/projects/",
        json={
            "id": str(project_id),
            "initiating_run_id": "telegram-id-barrier-opt-out-run",
            "title": "Built for someone else",
            "config": {},
        },
        headers={"X-Telegram-ID": str(sender_telegram_id)},
    )
    assert created.status_code == status.HTTP_201_CREATED, created.text

    denied = await async_client.post(
        f"/api/projects/{project_id}/config/bot-access",
        json={
            "mode": "only_me",
            "allowed_telegram_ids": str(DICTATED_TELEGRAM_ID),
            "allow_ownerless_audience": True,
        },
        headers={"X-Telegram-ID": str(sender_telegram_id)},
    )
    assert denied.status_code == status.HTTP_403_FORBIDDEN
    assert "internal service" in denied.json()["detail"]

    allowed = await async_client.post(
        f"/api/projects/{project_id}/config/bot-access",
        json={
            "mode": "only_me",
            "allowed_telegram_ids": str(DICTATED_TELEGRAM_ID),
            "allow_ownerless_audience": True,
        },
    )
    assert allowed.status_code == status.HTTP_200_OK, allowed.text
    assert allowed.json()["allowed_telegram_ids"] == str(DICTATED_TELEGRAM_ID)
