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

    # Host path to .claude directory (for mounting into workers)
    HOST_CLAUDE_DIR: str | None = None

    # Worker subprocess timeout (seconds)
    WORKER_SUBPROCESS_TIMEOUT_SECONDS: int = 300

    # Path to pre-scaffolded workspaces (created by scaffolder service)
    # All workspaces live here, keyed by repo_id: /data/workspaces/{repo_id}/
    SCAFFOLDED_WORKSPACE_PATH: str = "/data/workspaces"

    # Fixed name of the internal bridge network shared by all services
    INTERNAL_NETWORK: str = "codegen_internal"

    # Isolated network for worker containers (no access to orchestrator infra)
    WORKER_NETWORK: str = "codegen_worker"

    # URL of this worker-manager service (injected into worker containers)
    WORKER_MANAGER_URL: str = "http://worker-manager:8000"

    model_config = SettingsConfigDict(env_file=".env")


settings = WorkerManagerSettings()


def worker_urls(current_settings: WorkerManagerSettings | None = None) -> tuple[str, str]:
    """Return required worker-visible Redis and API URLs."""
    current_settings = current_settings or settings
    redis_url = current_settings.WORKER_REDIS_URL.strip()
    api_url = current_settings.WORKER_API_URL.strip()
    missing = [
        name
        for name, value in (("WORKER_REDIS_URL", redis_url), ("WORKER_API_URL", api_url))
        if not value
    ]
    if missing:
        raise RuntimeError(f"Required worker URL configuration is missing: {', '.join(missing)}")
    return redis_url, api_url
