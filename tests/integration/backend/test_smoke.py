import os

import httpx
import pytest

API_URL = os.getenv("API_URL", "http://api:8000")


@pytest.mark.asyncio
async def test_backend_integration_smoke():
    """Verify backend integration runner can reach API."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{API_URL}/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_api_seed_and_read_project(api_client, seed_project):
    """Verify we can seed a project via API and read it back."""
    created = await seed_project(name="Smoke Test", config={"modules": ["backend"]})
    pid = created["id"]
    assert created["status"] == "draft"

    resp = await api_client.get(f"/api/projects/{pid}")
    assert resp.status_code == 200
    project = resp.json()
    assert project["name"] == "Smoke Test"
    assert project["config"]["modules"] == ["backend"]
