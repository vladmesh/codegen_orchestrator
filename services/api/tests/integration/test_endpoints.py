"""Integration tests for API endpoints."""

import http

from httpx import ASGITransport, AsyncClient
import pytest

from src.main import app


@pytest.fixture
async def client():
    """Create test client."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_health(client):
    """Test health endpoint."""
    response = await client.get("/health")
    assert response.status_code == http.HTTPStatus.OK
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_root_endpoint(client):
    """Test root endpoint returns API info."""
    response = await client.get("/")
    assert response.status_code == http.HTTPStatus.OK
    data = response.json()
    assert "name" in data or "message" in data
