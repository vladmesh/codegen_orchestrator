from pydantic_settings import BaseSettings, SettingsConfigDict


class BrokerSettings(BaseSettings):
    REDIS_URL: str = "redis://redis:6379/0"
    BROKER_INTERNAL_TOKEN: str
    SESSION_TTL_SECONDS: int = 3600
    STREAM_MAXLEN: int = 1000
    WORKER_MANAGER_URL: str = "http://worker-manager:8000"
    model_config = SettingsConfigDict(env_file=".env")


settings = BrokerSettings()
