import asyncio
import shlex

import pytest

from shared import live_harness_cleanup


def test_remote_cleanup_command_keeps_project_name_as_argv() -> None:
    project_name = "live-test'\nrm -rf /"

    command = live_harness_cleanup.build_remote_cleanup_command(project_name)

    assert shlex.split(command) == ["sh", "-s", "--", project_name, "/opt/services"]


def test_registry_cleanup_uses_https_for_bare_registry_host(monkeypatch) -> None:
    requested_urls = []

    class Response:
        status_code = live_harness_cleanup.HTTP_NOT_FOUND

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            requested_urls.append(url)
            return Response()

    monkeypatch.setenv("ORCHESTRATOR_HOSTNAME", "registry.example.com")
    monkeypatch.setenv("REGISTRY_USER", "user")
    monkeypatch.setenv("REGISTRY_PASSWORD", "password")
    monkeypatch.setattr(live_harness_cleanup.httpx, "AsyncClient", lambda **kwargs: Client())

    asyncio.run(
        live_harness_cleanup.cleanup_registry_repository(
            repository="project-factory-organization/owned-repository-backend"
        )
    )

    assert requested_urls == [
        "https://registry.example.com/v2/"
        "project-factory-organization/owned-repository-backend/tags/list"
    ]


def test_registry_cleanup_fails_without_credentials(monkeypatch) -> None:
    monkeypatch.delenv("ORCHESTRATOR_HOSTNAME", raising=False)
    monkeypatch.setenv("REGISTRY_USER", "user")
    monkeypatch.setenv("REGISTRY_PASSWORD", "password")

    with pytest.raises(RuntimeError, match="credentials are not configured"):
        asyncio.run(live_harness_cleanup.cleanup_registry_repository(repository="repo"))
