import asyncio
import time
from typing import Any

from .agent_config import get_agent_config


class AgentConfigCache:
    """Simple TTL cache for agent configurations."""

    def __init__(self, ttl_seconds: int = 60):
        self.ttl = ttl_seconds
        self._cache: dict[str, tuple[dict[str, Any], float]] = {}
        self._lock = asyncio.Lock()

    async def get(self, agent_id: str) -> dict[str, Any]:
        """Get agent config from cache or fetch from API."""
        now = time.time()

        # Fast path reading
        if agent_id in self._cache:
            config, expiry = self._cache[agent_id]
            if now < expiry:
                return config

        # Slow path with lock to prevent thundering herd
        async with self._lock:
            # Double-check inside lock
            if agent_id in self._cache:
                config, expiry = self._cache[agent_id]
                if now < expiry:
                    return config

            # Fetch fresh config
            config = await get_agent_config(agent_id)
            # Validate that config has required fields
            if not config.get("llm_provider") or not config.get("model_identifier"):
                import structlog

                logger = structlog.get_logger()
                logger.warning(
                    "agent_config_missing_fields",
                    agent_id=agent_id,
                    has_llm_provider=bool(config.get("llm_provider")),
                    has_model_identifier=bool(config.get("model_identifier")),
                    config_keys=list(config.keys()),
                )
            self._cache[agent_id] = (config, now + self.ttl)
            return config

    def invalidate(self, agent_id: str | None = None) -> None:
        """Invalidate cache for one agent or all."""
        if agent_id:
            self._cache.pop(agent_id, None)
        else:
            self._cache.clear()


# Singleton instance
agent_config_cache = AgentConfigCache(ttl_seconds=60)
