"""Scheduler startup: validate system configs and expose ConfigStore.

Call init_config() once at startup before any workers start.
Other modules import `config` and use `config.get_int(...)`.
"""

import os

from shared.config_store import ConfigStore

# Module-level singleton — initialized by init_config()
config: ConfigStore | None = None

# All config keys required by the scheduler service
REQUIRED_KEYS = [
    "scheduler.dispatch_interval_seconds",
    "scheduler.github_sync_interval",
    "scheduler.github_sync_missing_threshold",
    "scheduler.server_sync_interval",
    "scheduler.server_details_sync_interval",
    "scheduler.provisioning_stuck_timeout_seconds",
    "scheduler.provisioning_trigger_cooldown_seconds",
    "scheduler.scaffold_inflight_ttl",
    "scheduler.ssl_check_timeout",
    "scheduler.rag_summarizer_poll_interval",
    "supervisor.story_stuck_threshold_minutes",
    "supervisor.task_stuck_threshold_minutes",
    "supervisor.story_max_architect_retries",
    "supervisor.story_retry_ttl",
    "health.ram_threshold_pct",
    "health.disk_threshold_pct",
    "health.consecutive_failure_threshold",
    "health.ssl_expiry_warning_days",
    "health.metrics_retention_hours",
    "health.metrics_cleanup_interval_seconds",
    "health.http_timeout",
]


def init_config() -> ConfigStore:
    """Initialize ConfigStore and validate all required keys.

    Raises RuntimeError if any required config is missing.
    Must be called before workers start.
    """
    global config  # noqa: PLW0603
    api_base_url = os.getenv("API_BASE_URL")
    if not api_base_url:
        raise RuntimeError("API_BASE_URL is not set")

    config = ConfigStore(api_base_url)
    config.validate_required(REQUIRED_KEYS)
    return config
