"""Service smoke tests for worker-manager compose stack."""

import os

import httpx


def test_service_health_smoke():
    response = httpx.get(f"{os.environ['WORKER_MANAGER_URL']}/health", timeout=5)

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
