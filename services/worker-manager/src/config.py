from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerManagerSettings(BaseSettings):
    ENVIRONMENT: str = "production"
    LOG_LEVEL: str = "INFO"
    REDIS_URL: str = "redis://redis:6379/0"

    # Worker config
    WORKER_IMAGE_PREFIX: str = "worker"
    WORKER_BASE_IMAGE: str = "worker-base:latest"
    WORKER_DOCKER_LABELS: str = "{}"  # JSON string

    # Network config
    # If set, workers attach to this Docker network (for DIND/integration tests)
    # If empty/None, workers use host networking (production default)
    DOCKER_NETWORK: str = ""

    # Worker-visible URLs (for DIND where workers can't resolve docker-compose DNS)
    # If not set, uses REDIS_URL. Set to IP-based URL for DIND testing.
    WORKER_REDIS_URL: str = ""
    WORKER_API_URL: str = ""

    model_config = SettingsConfigDict(env_file=".env")


settings = WorkerManagerSettings()
