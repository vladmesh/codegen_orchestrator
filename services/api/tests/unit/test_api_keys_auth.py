"""The api-keys router hands out decrypted platform credentials.

It answers with a cleartext provider key, so an unauthenticated caller must never
reach the handler. Workers share a network with the API, which makes this the
difference between an encrypted secret store and a public one.
"""

from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from src.database import get_async_session
from src.main import app


@pytest.fixture(autouse=True)
def _override_session():
    session = AsyncMock()

    async def _execute(query):
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        return result

    session.execute = _execute

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_api_key_without_credentials_is_401():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.get("/api/api-keys/time4vps")

    assert resp.status_code == HTTPStatus.UNAUTHORIZED


@pytest.mark.asyncio
async def test_create_api_key_without_credentials_is_401():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/api/api-keys/",
            json={"service": "time4vps", "value": "secret", "type": "credentials"},
        )

    assert resp.status_code == HTTPStatus.UNAUTHORIZED


@pytest.mark.asyncio
async def test_internal_service_still_reaches_the_handler():
    """The scheduler is the only caller; a missing key must be its 404, not a 401."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.get(
            "/api/api-keys/time4vps",
            headers={"X-Internal-Key": "test-internal-key"},
        )

    assert resp.status_code == HTTPStatus.NOT_FOUND
