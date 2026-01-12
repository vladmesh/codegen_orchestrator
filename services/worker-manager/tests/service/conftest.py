import pytest
import json
from unittest.mock import MagicMock
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
