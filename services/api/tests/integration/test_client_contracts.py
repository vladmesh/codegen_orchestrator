"""Contract tests for service-specific API clients.

These tests ensure the API exposes all endpoints used by other services.
If an endpoint path or method changes, these tests should fail.
"""

import pytest

from src.main import app


def _assert_route(method: str, path: str) -> None:
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return
    raise AssertionError(f"Missing route: {method} {path}")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        # Telegram bot
        ("POST", "/api/users/upsert"),
        ("POST", "/api/rag/messages"),
        ("GET", "/api/projects/"),
        ("GET", "/api/projects/{project_id}"),
        ("GET", "/api/servers/"),
        # LangGraph
        ("GET", "/api/agent-configs/{config_id}"),
        ("GET", "/api/cli-agent-configs/{config_id}"),
        ("POST", "/api/projects/"),
        ("PATCH", "/api/projects/{project_id}"),
        ("GET", "/api/servers/{handle}"),
        ("PATCH", "/api/servers/{handle}"),
        ("GET", "/api/servers/{handle}/services"),
        ("GET", "/api/servers/{handle}/ports"),
        ("POST", "/api/servers/{handle}/ports"),
        ("POST", "/api/service-deployments/"),
        ("POST", "/api/rag/query"),
        ("POST", "/api/incidents/"),
        ("GET", "/api/incidents/"),
        ("GET", "/api/incidents/active"),
        ("PATCH", "/api/incidents/{incident_id}"),
        ("GET", "/api/users/by-telegram/{telegram_id}"),
        # Scheduler
        ("POST", "/api/rag/ingest"),
        # Shared notifications
        ("GET", "/api/users/"),
    ],
)
def test_service_contracts(method: str, path: str) -> None:
    """Verify expected routes exist in the API."""
    _assert_route(method, path)
