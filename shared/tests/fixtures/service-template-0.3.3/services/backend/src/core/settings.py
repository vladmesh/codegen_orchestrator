"""Application configuration loaded from environment variables."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Base settings for the backend application."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = Field(validation_alias="APP_NAME")
    environment: str = Field(validation_alias="APP_ENV")
    app_secret_key: str = Field(validation_alias="APP_SECRET_KEY")
    debug: bool = Field(default=False, validation_alias="DEBUG")
