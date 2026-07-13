"""Pytest configuration for scheduler tests."""

import os
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

# Provide required env vars BEFORE any scheduler imports
# (api_client is instantiated at module level and calls get_settings())
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("API_BASE_URL", "http://localhost:8000")

import pytest  # noqa: E402

# Add scheduler src to path for imports
src_path = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(src_path))

# Add project root for shared imports
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))

from shared.tests.mocks.github import MockGitHubClient  # noqa: E402


@pytest.fixture(autouse=True)
def initialized_scheduler_config(monkeypatch):
    """Provide the validated runtime config expected by scheduler task tests."""
    from src import startup

    values = {
        "scheduler.dispatch_interval_seconds": 30,
        "scheduler.github_sync_interval": 300,
        "scheduler.github_sync_missing_threshold": 3,
        "scheduler.server_sync_interval": 60,
        "scheduler.server_details_sync_interval": 300,
        "scheduler.provisioning_stuck_timeout_seconds": 1800,
        "scheduler.provisioning_trigger_cooldown_seconds": 120,
        "scheduler.scaffold_inflight_ttl": 600,
        "scheduler.service_template_source": "gh:vladmesh/service-template",
        "scheduler.service_template_ref": "0.3.0",
        "scheduler.ssl_check_timeout": 5,
        "scheduler.rag_summarizer_poll_interval": 30,
        "deploy.max_deploy_retries": 3,
        "deploy.max_deploy_fix_attempts": 2,
        "deploy.deploy_retry_ttl": 86400,
        "supervisor.story_stuck_threshold_minutes": 5,
        "supervisor.task_stuck_threshold_minutes": 30,
        "supervisor.story_max_architect_retries": 3,
        "supervisor.story_retry_ttl": 3600,
        "health.ram_threshold_pct": 90.0,
        "health.disk_threshold_pct": 90.0,
        "health.consecutive_failure_threshold": 3,
        "health.ssl_expiry_warning_days": 7,
        "health.metrics_retention_hours": 168,
        "health.metrics_cleanup_interval_seconds": 86400,
        "health.http_timeout": 10.0,
    }
    config = MagicMock()
    config.get.side_effect = values.__getitem__
    config.get_int.side_effect = values.__getitem__
    config.get_float.side_effect = values.__getitem__
    monkeypatch.setattr(startup, "config", config)


@pytest.fixture
def mock_github():
    """Replace GitHubAppClient with mock for Scheduler tests."""
    mock = MockGitHubClient()

    with patch("shared.clients.github.GitHubAppClient", return_value=mock):
        yield mock
