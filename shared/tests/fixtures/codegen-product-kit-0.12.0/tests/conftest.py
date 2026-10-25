"""Pytest configuration for env_fixture.

This file contains shared fixtures and configuration for all tests.
"""

from collections.abc import Generator
import os

import pytest

try:
    from redis import Redis

    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False


if HAS_REDIS:

    @pytest.fixture(scope="session")
    def test_redis_url() -> str:
        """Get test Redis URL from environment."""
        redis_url = os.getenv("TEST_REDIS_URL")
        if redis_url:
            return redis_url
        redis_host = os.getenv("TEST_REDIS_HOST", "localhost")
        redis_port = os.getenv("TEST_REDIS_PORT", "6379")
        return f"redis://{redis_host}:{redis_port}/15"

    @pytest.fixture
    def redis_client(test_redis_url: str) -> Generator[Redis, None, None]:
        """Create a test Redis client with automatic cleanup."""
        client = Redis.from_url(test_redis_url, decode_responses=True)
        yield client
        client.flushdb()
        client.close()

# Add your custom fixtures below
