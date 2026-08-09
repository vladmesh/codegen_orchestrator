import pytest

from src.config import WorkerManagerSettings


def test_worker_broker_url_is_explicitly_configurable():
    settings = WorkerManagerSettings(
        WORKER_BROKER_URL="http://worker-broker.internal:8001",
        WORKER_BROKER_INTERNAL_TOKEN="test-internal-token",
    )

    assert settings.WORKER_BROKER_URL == "http://worker-broker.internal:8001"


def test_worker_broker_internal_token_is_required(monkeypatch):
    monkeypatch.delenv("WORKER_BROKER_INTERNAL_TOKEN", raising=False)
    with pytest.raises(ValueError, match="WORKER_BROKER_INTERNAL_TOKEN"):
        WorkerManagerSettings()
