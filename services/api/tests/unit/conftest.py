import os

os.environ.setdefault("WORKER_MANAGER_URL", "http://worker-manager:8000")
import sys

# Ensure /app is in path so 'src' can be imported inside Docker
sys.path.append("/app")

# Provide required env vars for Settings validation in unit tests
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault(
    "SECRETS_ENCRYPTION_KEY", "wHhIQWmPfLt60oHdxzbQhY1ZKnUon12e5_SuZ33xDxc="
)  # Valid Fernet key for tests only
os.environ.setdefault("LK_JWT_SECRET", "unit-test-lk-jwt-secret")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def api_source_index():
    """Parse the api source once, as session setup, for the guards that scan all of it."""
    from shared.tests import source_index

    api_src = Path(__file__).resolve().parents[2] / "src"
    for path in source_index.python_files(api_src):
        source_index.tree(path)
