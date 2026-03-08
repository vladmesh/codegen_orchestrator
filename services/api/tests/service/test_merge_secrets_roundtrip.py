"""Integration test: secrets round-trip persistence (#51).

Verifies that POST /config/secrets actually persists to the database
and the data survives a fresh GET (not just in-memory).
"""

from fastapi import status
from httpx import AsyncClient
import pytest

from shared.crypto import decrypt_dict


@pytest.mark.asyncio
async def test_secrets_roundtrip_persisted(async_client: AsyncClient):
    """POST secrets, GET project, verify secrets are persisted and decryptable."""
    # Seed user
    user_resp = await async_client.post(
        "/api/users/",
        json={"telegram_id": 300600, "username": "roundtrip_test_user"},
    )
    assert user_resp.status_code in (
        status.HTTP_201_CREATED,
        status.HTTP_400_BAD_REQUEST,
    )

    # Create project with existing config
    proj_resp = await async_client.post(
        "/api/projects/",
        json={
            "id": "00000000-0000-0000-0000-000000000003",
            "name": "Secrets Roundtrip Test",
            "status": "draft",
            "config": {"modules": ["backend"], "estimated_ram_mb": 512},
        },
        headers={"X-Telegram-ID": "300600"},
    )
    assert proj_resp.status_code in (
        status.HTTP_201_CREATED,
        status.HTTP_400_BAD_REQUEST,
    )

    # Merge secrets
    secrets_resp = await async_client.post(
        "/api/projects/00000000-0000-0000-0000-000000000003/config/secrets",
        json={
            "secrets": {"DB_URL": "postgres://localhost/mydb", "API_KEY": "sk-test-123"},
            "env_hints": {"DB_URL": "PostgreSQL connection string", "API_KEY": "OpenAI API key"},
        },
        headers={"X-Telegram-ID": "300600"},
    )
    assert secrets_resp.status_code == status.HTTP_200_OK  # noqa: PLR2004
    assert sorted(secrets_resp.json()["keys"]) == ["API_KEY", "DB_URL"]

    # Read back the project
    get_resp = await async_client.get(
        "/api/projects/00000000-0000-0000-0000-000000000003",
        headers={"X-Telegram-ID": "300600"},
    )
    assert get_resp.status_code == status.HTTP_200_OK  # noqa: PLR2004
    config = get_resp.json()["config"]

    # Verify secrets are encrypted and decryptable
    assert "secrets" in config, "secrets key missing from config after merge"
    decrypted = decrypt_dict(config["secrets"])
    assert decrypted["DB_URL"] == "postgres://localhost/mydb"
    assert decrypted["API_KEY"] == "sk-test-123"

    # Verify env_hints preserved
    assert config["env_hints"]["DB_URL"] == "PostgreSQL connection string"
    assert config["env_hints"]["API_KEY"] == "OpenAI API key"

    # Verify original config keys preserved
    assert config["modules"] == ["backend"]
    assert config["estimated_ram_mb"] == 512  # noqa: PLR2004


@pytest.mark.asyncio
async def test_secrets_merge_additive(async_client: AsyncClient):
    """Second POST adds to existing secrets, doesn't overwrite them."""
    # Reuse project from previous test (or create if needed)
    proj_resp = await async_client.post(
        "/api/projects/",
        json={
            "id": "00000000-0000-0000-0000-000000000004",
            "name": "Secrets Additive Test",
            "status": "draft",
            "config": {},
        },
        headers={"X-Telegram-ID": "300600"},
    )
    assert proj_resp.status_code in (
        status.HTTP_201_CREATED,
        status.HTTP_400_BAD_REQUEST,
    )

    # First merge
    await async_client.post(
        "/api/projects/00000000-0000-0000-0000-000000000004/config/secrets",
        json={"secrets": {"KEY_A": "val-a"}},
        headers={"X-Telegram-ID": "300600"},
    )

    # Second merge with different key
    resp2 = await async_client.post(
        "/api/projects/00000000-0000-0000-0000-000000000004/config/secrets",
        json={"secrets": {"KEY_B": "val-b"}},
        headers={"X-Telegram-ID": "300600"},
    )
    assert resp2.status_code == status.HTTP_200_OK  # noqa: PLR2004

    # Read back — both keys must be present
    get_resp = await async_client.get(
        "/api/projects/00000000-0000-0000-0000-000000000004",
        headers={"X-Telegram-ID": "300600"},
    )
    config = get_resp.json()["config"]
    decrypted = decrypt_dict(config["secrets"])
    assert "KEY_A" in decrypted, f"KEY_A lost after second merge! Got: {sorted(decrypted.keys())}"
    assert "KEY_B" in decrypted
