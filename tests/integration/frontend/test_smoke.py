import os

import httpx
import pytest

API_URL = os.getenv("API_URL", "http://api:8000")


@pytest.mark.asyncio
async def test_integration_api_health():
    """Verify API is reachable from integration test runner."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{API_URL}/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
