from pydantic import Field

from shared.config import BaseSettings


class Config(BaseSettings):
    """Orchestrator CLI Configuration."""

    api_url: str = Field(
        ..., alias="ORCHESTRATOR_API_URL", description="URL of the Orchestrator API"
    )

    redis_url: str = Field(
        ..., alias="ORCHESTRATOR_REDIS_URL", description="URL of the Redis instance"
    )

    worker_manager_url: str = Field(
        ...,
        alias="ORCHESTRATOR_WORKER_MANAGER_URL",
        description="URL of the Worker Manager service",
    )

    telegram_id: str | None = Field(
        None,
        alias="ORCHESTRATOR_TELEGRAM_ID",
        description="Telegram ID for user-scoped API requests",
    )
