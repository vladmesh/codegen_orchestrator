from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerManagerSettings(BaseSettings):
    ENVIRONMENT: str = "production"
    LOG_LEVEL: str = "INFO"
    REDIS_URL: str = "redis://redis:6379/0"

    # Worker config
    WORKER_IMAGE_PREFIX: str = "worker"
    WORKER_DOCKER_LABELS: str = "{}"  # JSON string

    model_config = SettingsConfigDict(env_file=".env")


settings = WorkerManagerSettings()
