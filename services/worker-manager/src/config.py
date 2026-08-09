from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerManagerSettings(BaseSettings):
    ENVIRONMENT: str = "production"
    LOG_LEVEL: str = "INFO"
    REDIS_URL: str = "redis://redis:6379/0"
    API_BASE_URL: str = "http://api:8000"

    # Worker config
    WORKER_IMAGE_PREFIX: str = "worker"
    WORKER_BASE_IMAGE: str = "worker-base:latest"
    WORKER_DOCKER_LABELS: str = "{}"  # JSON string

    # Network config
    # If set, workers attach to this Docker network (for DIND/integration tests).
    # If empty, workers attach to WORKER_NETWORK. Host networking is test-only.
    DOCKER_NETWORK: str = ""

    # The broker is the sole worker-visible control-plane transport.
    WORKER_BROKER_URL: str = "http://worker-broker:8001"
    WORKER_BROKER_INTERNAL_TOKEN: str
    WORKER_BROKER_SESSION_TTL_SECONDS: int = 3600

    # Host path to .claude directory (for mounting into workers)
    HOST_CLAUDE_DIR: str | None = None

    # Dedicated host Codex profile. It must not point at the operator's live
    # ~/.codex directory. The validation path is the same profile mounted
    # read-only into worker-manager by Compose.
    HOST_CODEX_HOME: str | None = None
    HOST_CODEX_VALIDATION_PATH: str | None = None

    # Worker subprocess timeout (seconds). Live LLM agents (Claude/Factory) need
    # well over the noop budget to write and iterate on real code; keep within the
    # harness LLM_ENGINEERING_TIMEOUT. The noop runner uses its own short timeout.
    WORKER_SUBPROCESS_TIMEOUT_SECONDS: int = 900

    # Path to pre-scaffolded workspaces (created by scaffolder service)
    # All workspaces live here, keyed by repo_id: /data/workspaces/{repo_id}/
    SCAFFOLDED_WORKSPACE_PATH: str = "/data/workspaces"

    # Fixed name of the internal bridge network shared by all services
    INTERNAL_NETWORK: str = "codegen_internal"

    # Isolated network for worker containers (no access to orchestrator infra)
    WORKER_NETWORK: str = "codegen_worker"


    # Host-backed artifacts survive worker container deletion. Operators set
    # retention explicitly; cleanup is best-effort and never blocks work.
    WORKER_TRANSCRIPT_STORAGE_PATH: str = "/data/worker-transcripts"
    WORKER_TRANSCRIPT_RETENTION_DAYS: int = 30
    WORKER_TRANSCRIPT_MAX_BYTES: int = 5 * 1024 * 1024

    model_config = SettingsConfigDict(env_file=".env")


settings = WorkerManagerSettings()
