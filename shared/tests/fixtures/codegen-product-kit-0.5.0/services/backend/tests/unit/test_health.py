"""Smoke tests verifying the FastAPI stack is wired correctly."""

from fastapi import status
from httpx import AsyncClient
import pytest


@pytest.mark.asyncio
async def test_health_endpoint_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}
