from unittest.mock import MagicMock

import pytest

from src import startup
from src.tasks import supervisor, task_dispatcher


def test_task_modules_read_config_initialized_after_import(monkeypatch):
    config = MagicMock()
    config.get_int.side_effect = lambda key: {
        "scheduler.dispatch_interval_seconds": 47,
        "deploy.max_deploy_retries": 9,
    }[key]
    monkeypatch.setattr(startup, "config", config)

    assert task_dispatcher._dispatch_interval() == 47
    assert supervisor._max_deploy_retries() == 9


def test_task_modules_fail_before_scheduler_config_initialization(monkeypatch):
    monkeypatch.setattr(startup, "config", None)

    with pytest.raises(RuntimeError, match="Scheduler config is not initialized"):
        task_dispatcher._dispatch_interval()


def test_required_keys_cover_every_scheduler_task_config_value():
    assert {
        "scheduler.dispatch_interval_seconds",
        "scheduler.github_sync_interval",
        "scheduler.github_sync_missing_threshold",
        "scheduler.server_sync_interval",
        "scheduler.server_details_sync_interval",
        "scheduler.provisioning_stuck_timeout_seconds",
        "scheduler.provisioning_trigger_cooldown_seconds",
        "scheduler.scaffold_inflight_ttl",
        "scheduler.service_template_source",
        "scheduler.service_template_ref",
        "scheduler.ssl_check_timeout",
        "scheduler.rag_summarizer_poll_interval",
        "deploy.max_deploy_retries",
        "deploy.max_deploy_fix_attempts",
        "deploy.deploy_retry_ttl",
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
        "scheduler.ci_failure_max_fingerprint_attempts",
    } <= set(startup.REQUIRED_KEYS)
