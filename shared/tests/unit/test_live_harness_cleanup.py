import asyncio
import base64
import shlex
from types import SimpleNamespace

import httpx
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


def test_merge_file_set_probe_reads_paths_and_the_two_legacy_edit_targets(
    monkeypatch, capsys
) -> None:
    class GitHub:
        async def get_repo(self, *_args):
            return SimpleNamespace(default_branch="main")

        async def get_token(self, *_args):
            return "token"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            request = httpx.Request("GET", url)
            if url.endswith("/commits/merge"):
                return httpx.Response(
                    200,
                    json={
                        "sha": "merge",
                        "parents": [{"sha": "parent"}],
                        "files": [
                            {"filename": "Makefile"},
                            {"filename": "AGENTS.md"},
                            {"filename": "product.py"},
                        ],
                    },
                    request=request,
                )
            if "/compare/main...merge" in url:
                return httpx.Response(200, json={"status": "behind"}, request=request)
            path = url.rsplit("/contents/", 1)[1]
            ref = kwargs["params"]["ref"]
            contents = {
                ("Makefile", "merge"): "all:\n\t@true\n",
                ("AGENTS.md", "merge"): "# Product instructions\n",
                ("AGENTS.md", "parent"): "# Product instructions\n",
            }
            return httpx.Response(
                200,
                json={
                    "encoding": "base64",
                    "content": base64.b64encode(contents[path, ref].encode()).decode(),
                },
                request=request,
            )

    monkeypatch.setattr(live_harness_cleanup, "GitHubAppClient", GitHub)
    monkeypatch.setattr(live_harness_cleanup.httpx, "AsyncClient", lambda **_kwargs: Client())

    payload = asyncio.run(
        live_harness_cleanup.probe_merge_file_set(
            owner="org", repo="repo", merge_commit_sha="merge"
        )
    )

    assert payload == {
        "merge_commit_sha": "merge",
        "default_branch": "main",
        "merged_into_default_branch": True,
        "parent_shas": ["parent"],
        "changed_paths": ["AGENTS.md", "Makefile", "product.py"],
        "file_contents": {
            "Makefile": "all:\n\t@true\n",
            "AGENTS.md": "# Product instructions\n",
        },
        "parent_file_contents": {"AGENTS.md": "# Product instructions\n"},
    }
    assert live_harness_cleanup.MERGE_FILE_SET_PROBE_MARKER in capsys.readouterr().out
