"""Integration test: concurrent secret writes must not lose data.

Verifies that the SELECT FOR UPDATE locking in POST /config/secrets
prevents race conditions when multiple parallel requests set different keys.
"""

import asyncio

from fastapi import status
from httpx import AsyncClient
import pytest

from shared.crypto import decrypt_dict


@pytest.mark.asyncio
async def test_concurrent_secret_writes_preserve_all_keys(async_client: AsyncClient):
    """Fire N parallel secret writes and verify all keys are present."""
    # Seed user
    user_resp = await async_client.post(
        "/api/users/",
        json={"telegram_id": 200500, "username": "concurrent_test_user"},
    )
    assert user_resp.status_code in (
        status.HTTP_201_CREATED,
        status.HTTP_400_BAD_REQUEST,  # already exists from prior run
    )

    # Create project
    proj_resp = await async_client.post(
        "/api/projects/",
        json={
            "id": "concurrent-secrets-test",
            "name": "Concurrent Secrets Test",
            "status": "created",
            "config": {},
        },
        headers={"X-Telegram-ID": "200500"},
    )
    assert proj_resp.status_code in (
        status.HTTP_201_CREATED,
        status.HTTP_400_BAD_REQUEST,  # already exists
    )

    # Fire 5 parallel secret writes with different keys
    num_keys = 5

    async def set_secret(i: int):
        return await async_client.post(
            "/api/projects/concurrent-secrets-test/config/secrets",
            json={
                "secrets": {f"KEY_{i}": f"value_{i}"},
                "env_hints": {f"KEY_{i}": f"hint_{i}"},
            },
            headers={"X-Telegram-ID": "200500"},
        )

    responses = await asyncio.gather(*[set_secret(i) for i in range(num_keys)])

    # All should succeed
    for i, resp in enumerate(responses):
        assert resp.status_code == status.HTTP_200_OK, (  # noqa: PLR2004
            f"Secret write {i} failed: {resp.text}"
        )

    # Read back the project and verify all keys are present
    proj_resp = await async_client.get(
        "/api/projects/concurrent-secrets-test",
        headers={"X-Telegram-ID": "200500"},
    )
    assert proj_resp.status_code == status.HTTP_200_OK  # noqa: PLR2004
    config = proj_resp.json()["config"]

    # Decrypt and verify all keys
    secrets = decrypt_dict(config.get("secrets", {}))
    for i in range(num_keys):
        assert f"KEY_{i}" in secrets, f"KEY_{i} missing! Only found: {sorted(secrets.keys())}"
        assert secrets[f"KEY_{i}"] == f"value_{i}"

    # Verify env_hints
    env_hints = config.get("env_hints", {})
    for i in range(num_keys):
        assert f"KEY_{i}" in env_hints, f"hint KEY_{i} missing!"
        assert env_hints[f"KEY_{i}"] == f"hint_{i}"
