"""Focused contract tests for manifest-backed product settings."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any, cast

from fastapi import status
from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.backend.src.app.models.setting import Setting
from services.backend.src.core.settings import get_settings
from services.backend.src.generated.settings_schemas import SETTINGS_SCHEMAS

SETTINGS_CAPABILITY_HEADER = "X-Settings-Capability"
FIRST_SUBJECT_VALUE = 2
SECOND_SUBJECT_VALUE = 3


@pytest.fixture(autouse=True)
def declared_settings() -> Generator[None, None, None]:
    """Give the generated core one representative manifest declaration."""
    SETTINGS_SCHEMAS.clear()
    SETTINGS_SCHEMAS.update(
        {
            "languages": {"type": "array", "items": {"type": "string"}},
            "digest_size": {"type": "integer", "minimum": 1, "maximum": 10},
        }
    )
    yield
    SETTINGS_SCHEMAS.clear()


def _headers(capability: str | None = None) -> dict[str, str]:
    return {
        SETTINGS_CAPABILITY_HEADER: capability or get_settings().settings_write_capability,
    }


async def _set(client: AsyncClient, payload: dict[str, Any]) -> dict[str, Any]:
    response = await client.post("/settings/set", headers=_headers(), json=payload)
    assert response.status_code == status.HTTP_200_OK
    return cast(dict[str, Any], response.json())


@pytest.mark.asyncio
async def test_settings_write_rejects_missing_duplicate_and_invalid_capabilities_before_mutation(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    payload = {"key": "languages", "value": ["ru", "en"]}

    missing = await client.post("/settings/set", json=payload)
    wrong = await client.post("/settings/set", headers=_headers("wrong"), json=payload)
    duplicate = await client.post(
        "/settings/set",
        headers=[(SETTINGS_CAPABILITY_HEADER, _headers()[SETTINGS_CAPABILITY_HEADER])] * 2,
        json=payload,
    )
    non_ascii = await client.post(
        "/settings/set",
        headers=[(SETTINGS_CAPABILITY_HEADER.encode(), b"\xff")],
        json=payload,
    )

    assert [response.status_code for response in (missing, wrong, duplicate, non_ascii)] == [
        status.HTTP_403_FORBIDDEN,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_403_FORBIDDEN,
    ]
    assert (await db_session.execute(select(Setting))).scalars().all() == []


@pytest.mark.asyncio
async def test_settings_schema_rejects_unknown_keys_and_invalid_values(client: AsyncClient) -> None:
    unknown = await client.post(
        "/settings/set", headers=_headers(), json={"key": "unknown", "value": "value"}
    )
    invalid = await client.post(
        "/settings/set", headers=_headers(), json={"key": "digest_size", "value": 11}
    )

    assert unknown.status_code == status.HTTP_404_NOT_FOUND
    assert invalid.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert "11" not in invalid.text


@pytest.mark.asyncio
async def test_settings_round_trip_is_idempotent_for_same_effective_value(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    payload = {"key": "languages", "value": ["ru", "en"]}

    first = await _set(client, payload)
    second = await _set(client, payload)
    fetched = await client.post("/settings/get", json={"key": "languages"})

    assert first == second == {
        "contract_version": 1,
        "key": "languages",
        "scope": "product",
        "subject_id": None,
        "value": ["ru", "en"],
    }
    assert fetched.status_code == status.HTTP_200_OK
    assert fetched.json() == first
    assert len((await db_session.execute(select(Setting))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_user_scoped_settings_are_isolated_by_subject(client: AsyncClient) -> None:
    first = await _set(
        client,
        {
            "key": "digest_size",
            "scope": "user",
            "subject_id": 1,
            "value": FIRST_SUBJECT_VALUE,
        },
    )
    second = await _set(
        client,
        {
            "key": "digest_size",
            "scope": "user",
            "subject_id": 2,
            "value": SECOND_SUBJECT_VALUE,
        },
    )
    first_read = await client.post(
        "/settings/get", json={"key": "digest_size", "scope": "user", "subject_id": 1}
    )
    invalid_scope = await client.post(
        "/settings/get", json={"key": "digest_size", "scope": "product", "subject_id": 1}
    )

    assert first["value"] == FIRST_SUBJECT_VALUE
    assert second["value"] == SECOND_SUBJECT_VALUE
    assert first_read.json() == first
    assert invalid_scope.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
