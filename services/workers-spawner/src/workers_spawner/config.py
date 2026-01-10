"""Configuration for workers-spawner service."""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Service settings from environment."""

    redis_url: str = "redis://localhost:6379"
    container_network: str = "codegen_orchestrator_internal"
    default_timeout_sec: int = 300
    default_ttl_hours: int = 2
    worker_image: str = "universal-worker:latest"

    # Redis channels
    command_channel: str = "cli-agent:commands"
    events_prefix: str = "agents"

    # Concurrency settings
    max_concurrent_handlers: int = 5  # Max parallel message handlers

    # Host path to Claude session directory (for volume mounting)
    # This MUST be the host path, not the container path
    host_claude_dir: str | None = None

    # API URL for orchestrator CLI to call (passed to containers)
    api_url: str = "http://api:8000"

    class Config:
        env_prefix = ""


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
