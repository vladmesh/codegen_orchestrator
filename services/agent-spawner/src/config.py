"""Configuration settings for Agent Spawner."""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Agent Spawner settings."""

    redis_url: str = "redis://localhost:6379"

    # Container settings
    agent_image: str = "agent-worker:latest"
    container_idle_timeout_sec: int = 300  # 5 min → pause
    container_destroy_timeout_sec: int = 86400  # 24h → destroy
    container_network: str = "codegen_orchestrator_internal"

    # Host path to Claude session directory (for volume mounting)
    # This MUST be the host path, not the container path
    host_claude_dir: str | None = None

    # Execution settings
    default_timeout_sec: int = 120

    class Config:
        env_prefix = ""


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
