"""Fixtures for infrastructure integration tests."""

import os

import httpx
import pytest
import redis.asyncio as redis

pytest_plugins = ("pytest_asyncio",)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
API_BASE_URL = os.getenv("API_BASE_URL", "http://api:8000")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY")


@pytest.fixture
async def redis_client():
    """Redis client for test operations."""
    client = redis.from_url(REDIS_URL, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
async def api_client():
    """HTTP client for API operations."""
    headers = {"X-Internal-Key": INTERNAL_API_KEY} if INTERNAL_API_KEY else {}
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=30.0, headers=headers) as client:
        yield client
