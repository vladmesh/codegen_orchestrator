from pathlib import Path

import pytest
import yaml

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


def test_worker_broker_internal_token_cannot_be_empty():
    with pytest.raises(ValueError, match="WORKER_BROKER_INTERNAL_TOKEN"):
        WorkerManagerSettings(WORKER_BROKER_INTERNAL_TOKEN="")


def test_service_compose_supplies_required_broker_internal_token():
    compose_path = Path(__file__).parents[4] / "docker/test/service/worker-manager.yml"
    compose = yaml.safe_load(compose_path.read_text())

    for service in ("worker-manager", "worker-manager-test-runner"):
        environment = compose["services"][service]["environment"]
        assert "WORKER_BROKER_INTERNAL_TOKEN=test-worker-broker-internal-token" in environment


def test_service_compose_supplies_internal_api_key_for_attempt_inventory():
    compose_path = Path(__file__).parents[4] / "docker/test/service/worker-manager.yml"
    compose = yaml.safe_load(compose_path.read_text())

    environment = compose["services"]["worker-manager"]["environment"]
    assert "INTERNAL_API_KEY=test-internal-api-key" in environment
