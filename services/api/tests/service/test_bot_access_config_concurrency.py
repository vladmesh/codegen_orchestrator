"""Concurrent bot-audience and generic config writes retain both updates."""

import asyncio
import uuid

from fastapi import status
from httpx import AsyncClient
import pytest
from sqlalchemy import select

from shared.crypto import encrypt_dict
from shared.models import Project


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "audience"),
    [
        ("only_me", "42"),
        ("public", ""),
        ("custom", "42,84"),
    ],
)
async def test_bot_access_endpoint_records_each_contract_audience(
    async_client: AsyncClient, mode: str, audience: str
):
    """The PO's access choices persist their exact template contract literal."""
    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = uuid.uuid4()

    user = await async_client.post(
        "/api/users/",
        json={"telegram_id": telegram_id, "username": f"bot_access_{telegram_id}"},
    )
    assert user.status_code == status.HTTP_201_CREATED

    created = await async_client.post(
        "/api/projects/",
        json={
            "initiating_run_id": "test-run-1",
            "id": str(project_id),
            "title": "Bot Access",
            "config": {},
        },
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
        "agent_type": "claude",
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
        json={
            "initiating_run_id": "test-run-1",
            "id": str(project_id),
            "title": "Concurrent Bot Access",
            "config": {},
        },
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
        "agent_type": "claude",
        "tree": "src/",
        "bot_access": {"mode": "only_me", "allowed_telegram_ids": str(telegram_id)},
        "env_overrides": {"TG_BOT_ALLOWED_TELEGRAM_IDS": str(telegram_id)},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "audience"),
    [("only_me", "42"), ("public", ""), ("custom", "42,84")],
)
async def test_bot_access_selection_replaces_legacy_admin_secret(
    async_client: AsyncClient, db_session, mode: str, audience: str
):
    """An explicit contract policy wins over and removes the legacy private marker."""
    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = uuid.uuid4()

    user = await async_client.post(
        "/api/users/",
        json={"telegram_id": telegram_id, "username": f"legacy_access_{telegram_id}"},
    )
    assert user.status_code == status.HTTP_201_CREATED
    created = await async_client.post(
        "/api/projects/",
        json={
            "initiating_run_id": "test-run-1",
            "id": str(project_id),
            "title": "Legacy Bot Access",
            "config": {},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert created.status_code == status.HTTP_201_CREATED

    project = (
        await db_session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one()
    project.config = {"secrets": encrypt_dict({"ADMIN_TELEGRAM_ID": "42", "OTHER": "value"})}
    await db_session.commit()

    response = await async_client.post(
        f"/api/projects/{project_id}/config/bot-access",
        json={"mode": mode, "allowed_telegram_ids": audience},
    )

    assert response.status_code == status.HTTP_200_OK
    project = await async_client.get(f"/api/projects/{project_id}")
    assert project.status_code == status.HTTP_200_OK
    config = project.json()["config"]
    assert config["bot_access"] == {"mode": mode, "allowed_telegram_ids": audience}
    assert config["env_overrides"] == {"TG_BOT_ALLOWED_TELEGRAM_IDS": audience}
    assert "ADMIN_TELEGRAM_ID" not in config["secrets"]


@pytest.mark.asyncio
async def test_legacy_admin_secret_cannot_be_deleted_without_a_contract_policy(
    async_client: AsyncClient, db_session
):
    """Deleting the only legacy private marker cannot make the next deploy public."""
    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = uuid.uuid4()

    user = await async_client.post(
        "/api/users/",
        json={"telegram_id": telegram_id, "username": f"legacy_delete_{telegram_id}"},
    )
    assert user.status_code == status.HTTP_201_CREATED
    created = await async_client.post(
        "/api/projects/",
        json={
            "initiating_run_id": "test-run-1",
            "id": str(project_id),
            "title": "Legacy Delete",
            "config": {},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert created.status_code == status.HTTP_201_CREATED

    project = (
        await db_session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one()
    project.config = {"secrets": encrypt_dict({"ADMIN_TELEGRAM_ID": "42"})}
    await db_session.commit()

    response = await async_client.delete(
        f"/api/projects/{project_id}/config/secrets/ADMIN_TELEGRAM_ID"
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["detail"] == "bot access is managed through /config/bot-access"
