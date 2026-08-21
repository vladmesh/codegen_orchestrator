import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import yaml

os.environ.setdefault("HEALTH_CHECK_INTERVAL", "300")
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key")

from shared.config_store import ConfigStore, ConfigStoreUnavailableError
from src import main, startup
from src.tasks import supervisor, task_dispatcher

REPO_ROOT = Path(__file__).resolve().parents[4]


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
        "supervisor.story_max_architect_retries",
        "supervisor.story_retry_ttl",
        "supervisor.qa_failure_max_fingerprint_attempts",
        "supervisor.qa_max_fix_attempts",
        "health.ram_threshold_pct",
        "health.disk_threshold_pct",
        "health.consecutive_failure_threshold",
        "health.ssl_expiry_warning_days",
        "health.metrics_retention_hours",
        "health.metrics_cleanup_interval_seconds",
        "health.http_timeout",
        "scheduler.ci_failure_max_fingerprint_attempts",
        "scheduler.ci_failure_log_excerpt_lines",
    } <= set(startup.REQUIRED_KEYS)


def test_every_required_key_is_declared_in_the_seed_file():
    """A required key missing from the file never reaches the DB on deploy."""
    declared = {
        entry["key"]
        for entry in yaml.safe_load((REPO_ROOT / "scripts" / "system_configs.yaml").read_text())
    }

    assert set(startup.REQUIRED_KEYS) <= declared


class _ConfigResponder:
    """Answers the config read the store sends through the shared transport."""

    def __init__(self):
        self.status_code = 200
        self.json_body = {"key": "scheduler.dispatch_interval_seconds", "value": 30}
        self.error = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status_code, json=self.json_body)


def _config_transport(responder):
    """Route the store's sync client through `responder` instead of the network."""
    real_client = httpx.Client

    def factory(**kwargs):
        return real_client(transport=httpx.MockTransport(responder), **kwargs)

    return patch("shared.clients.internal_api.httpx.Client", factory)


def test_dispatch_interval_survives_the_config_api_going_away(monkeypatch):
    """A working loop keeps its last known value while the source is unreachable."""
    responder = _ConfigResponder()

    with _config_transport(responder):
        store = ConfigStore("http://api:8000", cache_ttl=0)
        monkeypatch.setattr(startup, "config", store)

        assert task_dispatcher._dispatch_interval() == 30

        responder.error = httpx.ConnectError("connection refused")
        assert task_dispatcher._dispatch_interval() == 30


def test_dispatch_interval_still_fails_loudly_when_the_key_is_gone(monkeypatch):
    responder = _ConfigResponder()
    responder.status_code = 404

    with _config_transport(responder):
        store = ConfigStore("http://api:8000")
        monkeypatch.setattr(startup, "config", store)

        with pytest.raises(KeyError, match="not found"):
            task_dispatcher._dispatch_interval()


@pytest.mark.asyncio
async def test_startup_retries_config_validation_until_api_is_available(monkeypatch):
    attempts = 0

    def validate_configs():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConfigStoreUnavailableError("System config API is unavailable")

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(main, "_validate_configs", validate_configs)
    monkeypatch.setattr(main.asyncio, "sleep", no_wait)

    await main.initialize_configs()

    assert attempts == 2
