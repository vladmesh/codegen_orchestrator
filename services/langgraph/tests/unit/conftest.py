"""Unit test configuration."""

import os
from pathlib import Path
import sys
from unittest.mock import MagicMock

import pytest

from shared.config_store import ConfigStore

# Add /app to sys.path so that 'src' module can be imported.
# This is needed because the volume mount for tests doesn't include the src module.
app_path = Path("/app")
if app_path.exists() and str(app_path) not in sys.path:
    sys.path.insert(0, str(app_path))

# Provide required env vars for Settings validation in unit tests
os.environ.setdefault("API_BASE_URL", "http://api:8000")


@pytest.fixture(autouse=True)
def mock_deploy_config_store(monkeypatch):
    """Keep deploy-consumer unit tests independent of the system-config API."""
    from src.consumers import deploy

    store = MagicMock(spec=ConfigStore)
    store.get_int.return_value = 3600
    monkeypatch.setattr(deploy, "_config", store)
    return store
