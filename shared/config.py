"""Base configuration with pydantic-settings.

This module provides a base Settings class that services inherit from.
Each service defines its own Settings with required fields specific to it.

Usage in service:
    from shared.config import BaseSettings
    from pydantic import Field

    class Settings(BaseSettings):
        database_url: str = Field(..., description="Required for this service")

    settings = Settings()
"""

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings as PydanticBaseSettings, SettingsConfigDict


class BaseSettings(PydanticBaseSettings):
    """Base application settings.

    All fields here are optional with sensible defaults.
    Services inherit this and make required fields mandatory.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # === Optional fields with defaults ===

    # Logging configuration
    service_name: str = Field(
        default="unknown",
        description="Service name for structured logging",
    )
    log_format: Literal["json", "console"] = Field(
        default="console",
        description="Log output format",
    )
    log_level: str = Field(
        default="INFO",
        description="Log level (DEBUG, INFO, WARNING, ERROR)",
    )

    # Rate limiting
    notification_rate_limit: int = Field(
        default=10,
        ge=1,
        description="Max notifications per hour per user",
    )

    # Cache TTL
    agent_config_cache_ttl: int = Field(
        default=60,
        ge=1,
        description="Agent config cache TTL in seconds",
    )

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Ensure log level is valid."""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper_v = v.upper()
        if upper_v not in valid_levels:
            raise ValueError(f"Invalid log level: {v}. Must be one of {valid_levels}")
        return upper_v


# === Field definitions for reuse in service configs ===


def database_url_field(required: bool = True):
    """Database URL field definition."""
    if required:
        return Field(
            ...,
            description="PostgreSQL connection URL",
            examples=["postgresql+asyncpg://user:pass@db:5432/dbname"],
        )
    return Field(
        default=None,
        description="PostgreSQL connection URL (optional)",
    )


def redis_url_field(required: bool = True):
    """Redis URL field definition."""
    if required:
        return Field(
            ...,
            description="Redis connection URL",
            examples=["redis://redis:6379"],
        )
    return Field(
        default=None,
        description="Redis connection URL (optional)",
    )


def api_url_field(required: bool = True):
    """Internal API URL field definition."""
    if required:
        return Field(
            default="http://api:8000/api",
            alias="API_URL",
            description="Internal API service URL (must include /api prefix)",
            examples=["http://api:8000/api"],
        )
    return Field(
        default=None,
        alias="API_URL",
        description="Internal API service URL (optional, must include /api prefix)",
    )


def telegram_token_field(required: bool = True):
    """Telegram bot token field definition."""
    if required:
        return Field(
            ...,
            description="Telegram Bot API token",
        )
    return Field(
        default="",
        description="Telegram Bot API token (optional)",
    )
