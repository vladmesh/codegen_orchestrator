"""Integration tests for the generated, capability-protected user authority."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

from fastapi import status
from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.backend.src.app.models.user import User, UserChannel, UserStatus
from services.backend.src.app.repositories.user import UserRepository
from services.backend.src.core.settings import get_settings
import shared.generated.events as events_module

USERS_CAPABILITY_HEADER = "X-Grant-Capability"


def _headers(capability: str | None = None) -> dict[str, str]:
    """Return a valid user-write header for the active test configuration."""
    capability = capability or get_settings().users_grant_capability
    return {USERS_CAPABILITY_HEADER: capability}


async def _grant(
    client: AsyncClient, channel: str = "telegram", external_id: str = "111"
) -> dict[str, Any]:
    response = await client.post(
        "/users/grant",
        headers=_headers(),
        json={"channel": channel, "external_id": external_id},
    )
    assert response.status_code == status.HTTP_200_OK
    return cast(dict[str, Any], response.json())


async def _revoke(
    client: AsyncClient, channel: str = "telegram", external_id: str = "111"
) -> dict[str, Any]:
    response = await client.post(
        "/users/revoke",
        headers=_headers(),
        json={"channel": channel, "external_id": external_id},
    )
    assert response.status_code == status.HTTP_200_OK
    return cast(dict[str, Any], response.json())


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/users/grant", "/users/revoke"])
async def test_user_writes_reject_invalid_capability_before_writing(
    client: AsyncClient, path: str
) -> None:
    payload = {"channel": "telegram", "external_id": "111"}
    if path == "/users/revoke":
        await _grant(client)

    missing = await client.post(path, json=payload)
    wrong = await client.post(path, headers=_headers("wrong"), json=payload)
    malformed = await client.post(
        path,
        headers=[(USERS_CAPABILITY_HEADER, _headers()[USERS_CAPABILITY_HEADER])] * 2,
        json=payload,
    )
    non_ascii = await client.post(
        path,
        headers=[(USERS_CAPABILITY_HEADER.encode(), b"\xff")],
        json=payload,
    )

    assert missing.status_code == status.HTTP_403_FORBIDDEN
    assert wrong.status_code == status.HTTP_403_FORBIDDEN
    assert malformed.status_code == status.HTTP_403_FORBIDDEN
    assert non_ascii.status_code == status.HTTP_403_FORBIDDEN

    resolved = await client.get(
        "/users/access", params={"channel": "telegram", "external_id": "111"}
    )
    if path == "/users/grant":
        assert resolved.status_code == status.HTTP_404_NOT_FOUND
    else:
        assert resolved.status_code == status.HTTP_200_OK
        assert resolved.json()["status"] == "active"


@pytest.mark.asyncio
async def test_grant_is_idempotent_and_activates_the_identity(client: AsyncClient) -> None:
    first = await _grant(client)
    second = await _grant(client)

    assert second == first
    assert first["status"] == "active"

    resolved = await client.get(
        "/users/access", params={"channel": "telegram", "external_id": "111"}
    )
    assert resolved.status_code == status.HTTP_200_OK
    assert resolved.json() == first


@pytest.mark.asyncio
async def test_revoke_is_idempotent_and_grant_reactivates_the_identity(
    client: AsyncClient,
) -> None:
    granted = await _grant(client)

    first_revoke = await _revoke(client)
    second_revoke = await _revoke(client)

    assert first_revoke == granted | {"status": "inactive"}
    assert second_revoke == first_revoke

    resolved = await client.get(
        "/users/access", params={"channel": "telegram", "external_id": "111"}
    )
    assert resolved.status_code == status.HTTP_200_OK
    assert resolved.json() == first_revoke

    reactivated = await _grant(client)
    assert reactivated == granted


@pytest.mark.asyncio
async def test_revoke_unknown_identity_fails_closed_without_writing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await client.post(
        "/users/revoke",
        headers=_headers(),
        json={"channel": "telegram", "external_id": "unknown"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    identities = (await db_session.execute(select(UserChannel))).scalars().all()
    assert identities == []


@pytest.mark.asyncio
async def test_revoke_validates_channel_identity(client: AsyncClient) -> None:
    response = await client.post(
        "/users/revoke",
        headers=_headers(),
        json={"channel": "", "external_id": ""},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.asyncio
async def test_resolve_reports_active_inactive_and_unknown_identities(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    active = await _grant(client, external_id="active")

    inactive_user = User(status=UserStatus.INACTIVE)
    inactive_identity = UserChannel(
        user=inactive_user,
        channel="telegram",
        external_id="inactive",
    )
    db_session.add(inactive_identity)
    await db_session.flush()

    active_result = await client.get(
        "/users/access", params={"channel": "telegram", "external_id": "active"}
    )
    inactive_result = await client.get(
        "/users/access", params={"channel": "telegram", "external_id": "inactive"}
    )
    unknown_result = await client.get(
        "/users/access", params={"channel": "telegram", "external_id": "unknown"}
    )

    assert active_result.json() == active
    assert inactive_result.status_code == status.HTTP_200_OK
    assert inactive_result.json()["status"] == "inactive"
    assert unknown_result.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_grant_validates_channel_identity(client: AsyncClient) -> None:
    response = await client.post(
        "/users/grant",
        headers=_headers(),
        json={"channel": "", "external_id": ""},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.asyncio
async def test_grant_publishes_user_granted(client: AsyncClient) -> None:
    granted = await _grant(client, external_id="222")

    publish = cast(AsyncMock, events_module.get_broker().publish)
    publish.assert_awaited_once()
    await_args = publish.await_args
    assert await_args is not None
    (event,) = await_args.args
    assert await_args.kwargs == {"stream": "user_granted"}
    assert event.schema_version == 1
    assert event.payload.user_id == granted["user_id"]
    assert event.payload.status == "active"


@pytest.mark.asyncio
async def test_removed_crud_routes_are_not_exposed(client: AsyncClient) -> None:
    assert (await client.get("/users")).status_code == status.HTTP_404_NOT_FOUND
    assert (await client.post("/users", json={})).status_code == status.HTTP_404_NOT_FOUND
    assert (await client.patch("/users/1/status", json={"status": "active"})).status_code == (
        status.HTTP_404_NOT_FOUND
    )


@pytest.mark.asyncio
async def test_repository_grant_reactivates_an_inactive_identity(db_session: AsyncSession) -> None:
    user = User(status=UserStatus.INACTIVE)
    identity = UserChannel(user=user, channel="telegram", external_id="reactivate")
    db_session.add(identity)
    await db_session.flush()

    granted = await UserRepository(db_session).grant("telegram", "reactivate")

    assert granted.user.status == UserStatus.ACTIVE
