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
