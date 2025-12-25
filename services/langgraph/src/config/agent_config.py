"""Agent configuration fetcher with caching.

Fetches agent prompts and settings from the API with TTL caching
to avoid hitting the database on every LLM invocation.

IMPORTANT: No fallbacks - if API is unavailable, we fail fast.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Configuration
API_URL = os.getenv("API_URL", "http://api:8000")
CACHE_TTL_SECONDS = int(os.getenv("AGENT_CONFIG_CACHE_TTL", "60"))


class AgentConfigError(Exception):
    """Raised when agent config cannot be fetched."""
    pass


class AgentConfigCache:
    """TTL cache for agent configurations."""

    def __init__(self, ttl_seconds: int = CACHE_TTL_SECONDS):
        self._cache: dict[str, tuple[dict[str, Any], datetime]] = {}
        self._ttl = timedelta(seconds=ttl_seconds)
        self._lock = asyncio.Lock()

    def _is_expired(self, cached_at: datetime) -> bool:
        return datetime.utcnow() - cached_at > self._ttl

    async def get(self, agent_id: str) -> dict[str, Any]:
        """Get agent config from cache or fetch from API.
        
        Args:
            agent_id: Agent identifier (e.g., "product_owner", "brainstorm")
            
        Returns:
            Agent config dict with keys: id, name, system_prompt, model_name, temperature
            
        Raises:
            AgentConfigError: If config cannot be fetched
        """
        # Check cache first (without lock for read)
        if agent_id in self._cache:
            config, cached_at = self._cache[agent_id]
            if not self._is_expired(cached_at):
                logger.debug(f"Cache hit for agent config: {agent_id}")
                return config

        # Fetch with lock to prevent thundering herd
        async with self._lock:
            # Double-check after acquiring lock
            if agent_id in self._cache:
                config, cached_at = self._cache[agent_id]
                if not self._is_expired(cached_at):
                    return config

            # Fetch from API - fail fast if unavailable
            config = await self._fetch_from_api(agent_id)
            self._cache[agent_id] = (config, datetime.utcnow())
            return config

    async def _fetch_from_api(self, agent_id: str) -> dict[str, Any]:
        """Fetch agent config from API. Raises on failure."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{API_URL}/api/agent-configs/{agent_id}")
                
                if resp.status_code == 200:
                    logger.info(f"Fetched agent config from API: {agent_id}")
                    return resp.json()
                elif resp.status_code == 404:
                    raise AgentConfigError(
                        f"Agent config '{agent_id}' not found in database. "
                        f"Run 'make seed' to populate agent configs."
                    )
                else:
                    raise AgentConfigError(
                        f"Failed to fetch agent config '{agent_id}': "
                        f"HTTP {resp.status_code} - {resp.text}"
                    )
        except httpx.RequestError as e:
            raise AgentConfigError(
                f"Cannot connect to API to fetch agent config '{agent_id}': {e}. "
                f"Ensure API service is running."
            ) from e

    def invalidate(self, agent_id: str | None = None) -> None:
        """Invalidate cache.
        
        Args:
            agent_id: Specific agent to invalidate, or None to clear all
        """
        if agent_id:
            self._cache.pop(agent_id, None)
        else:
            self._cache.clear()


# Global cache instance
_cache = AgentConfigCache()


async def get_agent_config(agent_id: str) -> dict[str, Any]:
    """Get agent configuration with caching.
    
    Args:
        agent_id: Agent identifier
        
    Returns:
        Config dict
        
    Raises:
        AgentConfigError: If config cannot be fetched
    """
    return await _cache.get(agent_id)


def invalidate_cache(agent_id: str | None = None) -> None:
    """Invalidate agent config cache."""
    _cache.invalidate(agent_id)
