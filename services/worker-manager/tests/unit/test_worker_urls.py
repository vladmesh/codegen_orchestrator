from unittest.mock import MagicMock

import pytest

from src import config


def test_worker_urls_return_explicit_configured_values(monkeypatch):
    settings = MagicMock(WORKER_REDIS_URL="redis://worker-redis:6379/0", WORKER_API_URL="http://worker-api:8000")
    monkeypatch.setattr(config, "settings", settings)

    assert config.worker_urls() == ("redis://worker-redis:6379/0", "http://worker-api:8000")


@pytest.mark.parametrize(
    ("redis_url", "api_url", "missing"),
    [("", "http://api:8000", "WORKER_REDIS_URL"), ("redis://redis:6379/0", " ", "WORKER_API_URL")],
)
def test_worker_urls_reject_missing_required_values(monkeypatch, redis_url, api_url, missing):
    settings = MagicMock(WORKER_REDIS_URL=redis_url, WORKER_API_URL=api_url)
    monkeypatch.setattr(config, "settings", settings)

    with pytest.raises(RuntimeError, match=missing):
        config.worker_urls()
