from pydantic import Field
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
    WORKER_BROKER_INTERNAL_TOKEN: str = Field(min_length=1)
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

    # The QA executor's own network. It must be declared `internal: true`: a QA
    # executor is attached to this and to nothing else, so the deployment under
    # test is unreachable from its container rather than merely forbidden to it.
    # Worker creation fails closed if this network is missing or not internal.
    QA_EGRESS_NETWORK: str = "codegen_qa_egress"

    # The only destinations a QA run's egress proxy opens, per assigned agent.
    # Empty means the built-in defaults in `qa_egress.DEFAULT_MODEL_BACKENDS`.
    # Entries are comma-separated `host` or `host:port` (port defaults to 443).
    QA_CLAUDE_BACKEND_HOSTS: str = ""
    QA_CODEX_BACKEND_HOSTS: str = ""

    # Host-backed artifacts survive worker container deletion. Operators set
    # retention explicitly; cleanup is best-effort and never blocks work.
    WORKER_TRANSCRIPT_STORAGE_PATH: str = "/data/worker-transcripts"
    WORKER_TRANSCRIPT_RETENTION_DAYS: int = 30
    WORKER_TRANSCRIPT_MAX_BYTES: int = 5 * 1024 * 1024

    # How a worker ended is readable only while its container exists, so the
    # deletion path reads it first. That read is bounded and never owns the
    # deletion: past this budget the removal proceeds and the record says the
    # capture ran out of time. The record itself is run-scoped and outlives
    # `worker:meta`, so it needs a retention of its own.
    WORKER_REMOVAL_EVIDENCE_TIMEOUT_SECONDS: float = 10.0
    WORKER_REMOVAL_EVIDENCE_TTL_SECONDS: int = 14 * 24 * 3600

    model_config = SettingsConfigDict(env_file=".env")


settings = WorkerManagerSettings()
