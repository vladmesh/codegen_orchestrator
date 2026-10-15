import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import httpx
import pytest
import yaml

os.environ.setdefault("HEALTH_CHECK_INTERVAL", "60")
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key")

from shared.config_store import BoundedStalePolicy, ConfigStore, ConfigStoreUnavailableError
from src import runtime, startup
from src.tasks import task_dispatcher
from src.tasks.supervisor.deploy import _max_deploy_retries

REPO_ROOT = Path(__file__).resolve().parents[4]


def test_task_modules_read_config_initialized_after_import(monkeypatch):
    config = MagicMock()
    config.get_int.side_effect = lambda key: {
        "scheduler.dispatch_interval_seconds": 47,
        "deploy.max_deploy_retries": 9,
    }[key]
    monkeypatch.setattr(startup, "config", config)

    assert task_dispatcher._dispatch_interval() == 47
    assert _max_deploy_retries() == 9


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
        "deploy.max_deploy_retries",
        "deploy.max_deploy_fix_attempts",
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


def test_bounded_stale_keys_are_a_small_explicit_cadence_allowlist():
    assert startup.BOUNDED_STALE_KEYS == {
        "scheduler.dispatch_interval_seconds",
        "scheduler.github_sync_interval",
        "scheduler.server_sync_interval",
        "scheduler.server_details_sync_interval",
        "health.metrics_cleanup_interval_seconds",
    }
    assert startup.BOUNDED_STALE_KEYS <= set(startup.REQUIRED_KEYS)

    critical_keys = {
        "deploy.max_deploy_retries",
        "deploy.max_deploy_fix_attempts",
        "supervisor.story_max_architect_retries",
        "supervisor.qa_max_fix_attempts",
        "supervisor.temporary_access_ttl_minutes",
        "scheduler.provisioning_stuck_timeout_seconds",
        "health.consecutive_failure_threshold",
    }
    assert not startup.BOUNDED_STALE_KEYS & critical_keys


async def test_init_config_applies_bounded_stale_only_to_safe_owned_keys(monkeypatch):
    monkeypatch.setenv("API_BASE_URL", "http://api:8000")
    monkeypatch.setattr(startup, "config", None)
    store = MagicMock(refresh=AsyncMock())
    factory = MagicMock(return_value=store)
    monkeypatch.setattr(startup, "ConfigStore", factory)

    owned_keys = {
        "scheduler.dispatch_interval_seconds",
        "deploy.max_deploy_retries",
    }

    assert await startup.init_config(owned_keys) is store

    factory.assert_called_once_with(
        "http://api:8000",
        stale_policies={
            "scheduler.dispatch_interval_seconds": BoundedStalePolicy(
                max_age_seconds=startup.BOUNDED_STALE_MAX_AGE_SECONDS
            )
        },
    )
    assert store.mock_calls == [call.refresh(), call.validate_required(sorted(owned_keys))]


class _ConfigResponder:
    """Answers the batch config read the store sends through the shared transport."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status_code = 200
        self.values: dict = {"scheduler.dispatch_interval_seconds": 30}
        self.error = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        body = [{"key": key, "value": value} for key, value in self.values.items()]
        return httpx.Response(self.status_code, json=body)


def _config_transport(responder):
    """Route the store's async client through `responder` instead of the network."""
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        return real_client(transport=httpx.MockTransport(responder), **kwargs)

    return patch("shared.clients.internal_api.httpx.AsyncClient", factory)


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr("shared.config_store.time.monotonic", fake)
    return fake


async def test_loop_interval_accessors_read_the_startup_snapshot_without_http(
    monkeypatch, tmp_path
):
    responder = _ConfigResponder()
    responder.values = dict.fromkeys(startup.PIPELINE_REQUIRED_KEYS, 7)
    monkeypatch.setenv("API_BASE_URL", "http://api:8000")
    monkeypatch.setattr(startup, "config", None)
    monkeypatch.setattr(runtime, "READINESS_PATH", tmp_path / "scheduler-ready")

    with _config_transport(responder):
        await runtime.initialize_configs(
            startup.PIPELINE_REQUIRED_KEYS, service_name="scheduler-pipeline"
        )
        for _ in range(5):
            assert task_dispatcher._dispatch_interval() == 7
            assert _max_deploy_retries() == 7

    assert [request.url.path for request in responder.requests] == ["/api/system-configs/"]


async def test_dispatch_interval_survives_a_short_config_api_outage(monkeypatch, clock):
    """A cadence-only key may use its bounded last-known value."""
    responder = _ConfigResponder()

    with _config_transport(responder):
        store = ConfigStore(
            "http://api:8000",
            stale_policies=startup._stale_policies_for({"scheduler.dispatch_interval_seconds"}),
        )
        monkeypatch.setattr(startup, "config", store)
        await store.refresh()

        assert task_dispatcher._dispatch_interval() == 30

        clock.now += 60
        responder.error = httpx.ConnectError("connection refused")
        with pytest.raises(ConfigStoreUnavailableError):
            await store.refresh()
        assert task_dispatcher._dispatch_interval() == 30


async def test_deploy_retry_limit_fails_closed_when_config_api_goes_away(monkeypatch, clock):
    """Behavior-changing retry policy never inherits the cadence fallback."""
    responder = _ConfigResponder()
    responder.values = {"deploy.max_deploy_retries": 3}

    with _config_transport(responder):
        store = ConfigStore(
            "http://api:8000",
            stale_policies=startup._stale_policies_for({"deploy.max_deploy_retries"}),
        )
        monkeypatch.setattr(startup, "config", store)
        await store.refresh()

        assert _max_deploy_retries() == 3

        clock.now += 60
        responder.error = httpx.ConnectError("connection refused")
        with pytest.raises(ConfigStoreUnavailableError):
            await store.refresh()
        with pytest.raises(ConfigStoreUnavailableError, match="unavailable"):
            _max_deploy_retries()


async def test_dispatch_interval_still_fails_loudly_when_the_key_is_gone(monkeypatch):
    responder = _ConfigResponder()
    responder.values = {}

    with _config_transport(responder):
        store = ConfigStore(
            "http://api:8000",
            stale_policies=startup._stale_policies_for({"scheduler.dispatch_interval_seconds"}),
        )
        monkeypatch.setattr(startup, "config", store)
        await store.refresh()

        with pytest.raises(KeyError, match="not found"):
            task_dispatcher._dispatch_interval()


@pytest.mark.asyncio
async def test_startup_retries_config_validation_until_api_is_available(monkeypatch, tmp_path):
    attempts = 0
    marker = tmp_path / "scheduler-ready"
    marker.write_text("stale-process")

    async def validate_configs(_required_keys):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConfigStoreUnavailableError("System config API is unavailable")

    async def no_wait(_seconds):
        assert not marker.exists()

    monkeypatch.setattr(runtime, "READINESS_PATH", marker)
    monkeypatch.setattr(runtime.startup, "init_config", validate_configs)
    monkeypatch.setattr(runtime.asyncio, "sleep", no_wait)

    await runtime.initialize_configs(
        startup.PIPELINE_REQUIRED_KEYS, service_name="scheduler-pipeline"
    )

    assert attempts == 2
