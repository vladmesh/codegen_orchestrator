from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BrokerSettings(BaseSettings):
    REDIS_URL: str = Field(min_length=1)
    WORKER_BROKER_INTERNAL_TOKEN: str = Field(min_length=1)
    WORKER_BROKER_SESSION_TTL_SECONDS: int = 3600
    WORKER_BROKER_STREAM_MAXLEN: int = 1000
    WORKER_MANAGER_URL: str = Field(min_length=1)
    model_config = SettingsConfigDict(env_file=".env")


settings = BrokerSettings()
