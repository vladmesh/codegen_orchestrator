"""Fixtures for PO tools integration tests.

These tests run against a real API (with DB) via docker compose.
PO tools (langgraph) call the API — two services, true integration test.
"""

from __future__ import annotations

import os
import sys

import httpx
import pytest
from redis.asyncio import Redis

# Integration test runner has PYTHONPATH=/app with services/ mounted.
# Add langgraph src to path so PO tool imports resolve.
_LANGGRAPH_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "services", "langgraph")
_LANGGRAPH_DIR = os.path.normpath(_LANGGRAPH_DIR)
if _LANGGRAPH_DIR not in sys.path:
    sys.path.insert(0, _LANGGRAPH_DIR)

from shared.redis_client import RedisStreamClient  # noqa: E402
from src.agents.po.tools import init_po_clients  # noqa: E402

API_URL = os.getenv("API_URL", "http://localhost:8000")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

TEST_TELEGRAM_ID = 777000777


@pytest.fixture
async def api_client():
    """httpx.AsyncClient pointed at the real API."""
    async with httpx.AsyncClient(base_url=API_URL, timeout=10) as client:
        yield client


@pytest.fixture
async def redis_client():
    """Redis client for stream assertions."""
    client = Redis.from_url(REDIS_URL)
    yield client
    await client.aclose()


@pytest.fixture
async def stream_client(redis_client):
    """RedisStreamClient for PO tools."""
    return RedisStreamClient(redis_client)


@pytest.fixture
async def po_clients(api_client, stream_client):
    """Initialize PO tools with real clients."""
    init_po_clients(api_client, stream_client)
    yield
    init_po_clients(None, None)


@pytest.fixture
async def test_user(api_client):
    """Ensure a test user exists in the API, return telegram_id."""
    resp = await api_client.get(f"/api/users/by-telegram/{TEST_TELEGRAM_ID}")
    if resp.status_code == 404:
        resp = await api_client.post(
            "/api/users/",
            json={
                "telegram_id": TEST_TELEGRAM_ID,
                "username": "test_po_contract",
                "first_name": "Test",
                "is_admin": True,
            },
        )
        resp.raise_for_status()
    return TEST_TELEGRAM_ID


def make_config(user_id: str | int = TEST_TELEGRAM_ID) -> dict:
    """Create a RunnableConfig for PO tool invocation."""
    return {"configurable": {"thread_id": f"po-user-{user_id}", "user_id": str(user_id)}}
