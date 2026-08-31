
from httpx import AsyncClient
import pytest


@pytest.mark.asyncio
async def test_backend_health_endpoint():
    async with AsyncClient(base_url="http://backend:8000") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_backend_openapi_spec():
    async with AsyncClient(base_url="http://backend:8000") as client:
        response = await client.get("/openapi.json")
        assert response.status_code == 200
        spec = response.json()
        assert "openapi" in spec
        assert "info" in spec
        assert "paths" in spec

