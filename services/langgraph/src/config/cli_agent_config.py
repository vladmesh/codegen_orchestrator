"""CLI Agent configuration fetcher with caching.

Fetches CLI agent settings from the API with TTL caching.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any

import httpx
import structlog

from src.config.settings import get_settings

logger = structlog.get_logger(__name__)

# Get validated settings
_settings = get_settings()
CACHE_TTL_SECONDS = _settings.agent_config_cache_ttl


class CLIAgentConfigError(Exception):
    """Raised when CLI agent config cannot be fetched."""

    pass


class CLIAgentConfigCache:
    """TTL cache for CLI agent configurations."""

    def __init__(self, ttl_seconds: int = CACHE_TTL_SECONDS):
        self._cache: dict[str, tuple[dict[str, Any], datetime]] = {}
        self._ttl = timedelta(seconds=ttl_seconds)
        self._lock = asyncio.Lock()

    def _is_expired(self, cached_at: datetime) -> bool:
        return datetime.utcnow() - cached_at > self._ttl

    async def get(self, agent_id: str) -> dict[str, Any]:
        """Get CLI agent config from cache or fetch from API.

        Args:
            agent_id: Agent identifier (e.g., "architect.spawn_factory_worker")

        Returns:
            Config dict.

        Raises:
            CLIAgentConfigError: If config cannot be fetched
        """
        # Check cache first
        if agent_id in self._cache:
            config, cached_at = self._cache[agent_id]
            if not self._is_expired(cached_at):
                return config

        # Fetch with lock
        async with self._lock:
            if agent_id in self._cache:
                config, cached_at = self._cache[agent_id]
                if not self._is_expired(cached_at):
                    return config

            config = await self._fetch_from_api(agent_id)
            self._cache[agent_id] = (config, datetime.utcnow())
            return config

    async def _fetch_from_api(self, agent_id: str) -> dict[str, Any]:
        """Fetch CLI agent config from API."""
        try:
            from src.clients.api import api_client

            config = await api_client.get_cli_agent_config(agent_id)
            logger.info("cli_config_fetched", agent_id=agent_id)
            return config
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == httpx.codes.NOT_FOUND:
                raise CLIAgentConfigError(f"CLI Agent config '{agent_id}' not found.") from exc
            raise CLIAgentConfigError(
                f"Failed to fetch CLI agent config '{agent_id}': "
                f"HTTP {exc.response.status_code} - {exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            raise CLIAgentConfigError(
                f"Cannot connect to API to fetch CLI agent config '{agent_id}': {exc}"
            ) from exc

    def invalidate(self, agent_id: str | None = None) -> None:
        """Invalidate cache."""
        if agent_id:
            self._cache.pop(agent_id, None)
        else:
            self._cache.clear()


# Global cache instance
cli_agent_config_cache = CLIAgentConfigCache()
