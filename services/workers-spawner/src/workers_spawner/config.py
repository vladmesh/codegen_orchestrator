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

    class Config:
        env_prefix = ""


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
