"""Doubles for the unit suite only.

These live under `tests/unit/` deliberately. They replace worker path
preparation and broker registration with mocks, which is right for tests about
launch policy and wrong for a service test: a service test that talks to the
real broker must not be quietly served a mock instead.
"""

import pytest
import json
from unittest.mock import AsyncMock, MagicMock
from src.config import WorkerManagerSettings


@pytest.fixture
def mock_docker_client():
    """Mock docker client for service tests."""
    client = MagicMock()
    # Setup standard mock behaviors if needed
    return client


@pytest.fixture
def worker_settings(monkeypatch):
    """Force test settings."""
    monkeypatch.setenv("WORKER_IMAGE_PREFIX", "worker-test")
    monkeypatch.setenv("WORKER_DOCKER_LABELS", json.dumps({"com.codegen.environment": "test"}))
    return WorkerManagerSettings()


@pytest.fixture(autouse=True)
def mock_worker_path_preparation(monkeypatch):
    """Keep manager unit tests independent of host-owned bind mounts."""
    monkeypatch.setattr("src.manager.workspace_mod.prepare_worker_paths", MagicMock())


@pytest.fixture(autouse=True)
def mock_worker_broker_registration(monkeypatch):
    """Worker-manager units exercise launch policy, not a live broker HTTP service."""
    monkeypatch.setattr("src.manager.WorkerManager._register_broker_worker", AsyncMock())
    monkeypatch.setattr("src.manager.WorkerManager._unregister_broker_worker", AsyncMock())
