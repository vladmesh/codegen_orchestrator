"""Scheduler startup: validate process-owned configs and expose ConfigStore.

Call init_config() with the process key set before any workers start.
Other modules import `config` and use `config.get_int(...)`.
"""

from collections.abc import Collection
import os

from shared.config_store import BoundedStalePolicy, ConfigStore

# Module-level singleton — initialized by init_config()
config: ConfigStore | None = None

# Config ownership follows the process boundary. A process validates only the
# values its own loops can read, so an unrelated missing value cannot block it.
PIPELINE_REQUIRED_KEYS = {
    "scheduler.dispatch_interval_seconds",
    "scheduler.scaffold_inflight_ttl",
    "scheduler.service_template_source",
    "scheduler.service_template_ref",
    "scheduler.ci_failure_max_fingerprint_attempts",
    "scheduler.ci_failure_log_excerpt_lines",
    "deploy.max_deploy_retries",
    "deploy.max_deploy_fix_attempts",
    "deploy.deploy_retry_ttl",
    "supervisor.story_stuck_threshold_minutes",
    "supervisor.story_max_architect_retries",
    "supervisor.story_retry_ttl",
    "supervisor.qa_handoff_recovery_minutes",
    "supervisor.qa_failure_max_fingerprint_attempts",
    "supervisor.qa_max_fix_attempts",
    "supervisor.resource_wait_timeout_minutes",
    "supervisor.deploy_wait_max_minutes",
    "supervisor.qa_wait_max_minutes",
    "supervisor.pr_review_wait_max_minutes",
    "supervisor.user_secret_wait_max_minutes",
    "supervisor.stage_notice_quiet_minutes",
    "supervisor.resource_wait_metrics_freshness_seconds",
    "supervisor.qa_handoff_target_held_max_minutes",
    "supervisor.temporary_access_ttl_minutes",
    "supervisor.temporary_access_grant_stale_minutes",
    "supervisor.temporary_access_max_grant_attempts",
    "supervisor.temporary_access_revoke_stale_minutes",
    "supervisor.temporary_access_max_revoke_attempts",
    "supervisor.temporary_access_unrevoked_ttl_minutes",
}

INFRASTRUCTURE_REQUIRED_KEYS = {
    "scheduler.server_sync_interval",
    "scheduler.server_details_sync_interval",
    "scheduler.provisioning_stuck_timeout_seconds",
    "scheduler.provisioning_trigger_cooldown_seconds",
    "scheduler.ssl_check_timeout",
    "health.ram_threshold_pct",
    "health.disk_threshold_pct",
    "health.consecutive_failure_threshold",
    "health.ssl_expiry_warning_days",
    "health.metrics_retention_hours",
    "health.metrics_cleanup_interval_seconds",
    "health.http_timeout",
}

MAINTENANCE_REQUIRED_KEYS = {
    "scheduler.github_sync_interval",
    "scheduler.github_sync_missing_threshold",
    "scheduler.rag_summarizer_poll_interval",
}

REQUIRED_KEYS = sorted(
    PIPELINE_REQUIRED_KEYS | INFRASTRUCTURE_REQUIRED_KEYS | MAINTENANCE_REQUIRED_KEYS
)

# These values only control how often already-safe work is polled or cleaned up.
# Let a short config API outage preserve cadence, but do not let the scheduler run
# indefinitely on an old operator value. Everything not listed here fails closed.
BOUNDED_STALE_MAX_AGE_SECONDS = 15 * 60
BOUNDED_STALE_KEYS = frozenset(
    {
        "scheduler.dispatch_interval_seconds",
        "scheduler.github_sync_interval",
        "scheduler.server_sync_interval",
        "scheduler.server_details_sync_interval",
        "scheduler.rag_summarizer_poll_interval",
        "health.metrics_cleanup_interval_seconds",
    }
)


def _stale_policies_for(required_keys: Collection[str]) -> dict[str, BoundedStalePolicy]:
    """Return the bounded-stale policy for safe cadence keys owned by this process."""
    return {
        key: BoundedStalePolicy(max_age_seconds=BOUNDED_STALE_MAX_AGE_SECONDS)
        for key in required_keys
        if key in BOUNDED_STALE_KEYS
    }


def get_config() -> ConfigStore:
    """Return the initialized scheduler configuration store."""
    if config is None:
        raise RuntimeError(
            "Scheduler config is not initialized; call init_config() before starting workers"
        )
    return config


def init_config(required_keys: Collection[str]) -> ConfigStore:
    """Initialize ConfigStore and validate the requested process keys.

    Raises RuntimeError if any required config is missing.
    Must be called before workers start.
    """
    global config  # noqa: PLW0603
    api_base_url = os.getenv("API_BASE_URL")
    if not api_base_url:
        raise RuntimeError("API_BASE_URL is not set")

    owned_keys = frozenset(required_keys)
    config = ConfigStore(
        api_base_url,
        stale_policies=_stale_policies_for(owned_keys),
    )
    config.validate_required(sorted(owned_keys))
    return config
