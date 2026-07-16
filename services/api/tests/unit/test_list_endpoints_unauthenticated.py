"""Unauthenticated list endpoints must return 401, not crash.

Regression: the `status` query parameter shadowed the fastapi.status module,
so the 401 branch raised AttributeError and the API answered 500. The live
cleanup fence (scripts/clean_live_tests.py) hit exactly this.
"""

from http import HTTPStatus
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient
import pytest

from src.database import get_async_session
from src.main import app


@pytest.fixture(autouse=True)
def _override_session():
    session = AsyncMock()

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/runs/", "/api/projects/"])
async def test_unauthenticated_list_returns_401(path):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.get(path, params={"status": "running"})
    assert resp.status_code == HTTPStatus.UNAUTHORIZED
