"""The deploy operation owns one native GitHub HTTP pool on every exit path."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from shared.clients.github import GitHubAppClient
from src.deploy_fence import DeployFenceLost, DeployWrite
from src.subgraphs.devops import deployer


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "cancelled", "refused", "error", "cancel", "fence"])
async def test_deploy_reuses_and_closes_native_pool(monkeypatch, outcome):
    github = GitHubAppClient()
    clients, requests, guards = [], [], []
    native_client = httpx.AsyncClient

    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={})

    def make_client(*args, **kwargs):
        client = native_client(transport=httpx.MockTransport(respond), verify=False)
        clients.append(client)
        return client

    monkeypatch.setattr("shared.clients.github._base.httpx.AsyncClient", make_client)
    monkeypatch.setattr(deployer, "GitHubAppClient", lambda: github)
    node = deployer.DeployerNode()
    monkeypatch.setattr(node, "_run_cancelled", AsyncMock(return_value=outcome == "cancelled"))
    fence = SimpleNamespace(ensure_held=AsyncMock())

    async def read(*args):
        await github._make_request("GET", "https://api.github.com/workflow", {})

    async def write(*args, **kwargs):
        async def guard():
            await kwargs["deploy_fence"].ensure_held(DeployWrite.GITHUB_SECRETS)
            guards.append("write")

        await github._make_request("PUT", "https://api.github.com/secrets", {}, guard=guard)
        if outcome == "error":
            raise ValueError("synthetic failure")
        if outcome == "fence":
            raise DeployFenceLost("pool-project", DeployWrite.GITHUB_SECRETS)
        if outcome == "cancel":
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
        return outcome != "refused"

    async def dispatch(*args):
        await github._make_request("POST", "https://api.github.com/dispatch", {})
        return {"id": 7, "head_sha": "b" * 40}, False

    monkeypatch.setattr(deployer, "require_backend_workflow", read)
    monkeypatch.setattr(deployer, "write_deploy_secrets", write)
    monkeypatch.setattr(node, "_dispatch_and_wait", dispatch)
    monkeypatch.setattr(node, "_published_images", AsyncMock(return_value=({}, None)))
    monkeypatch.setattr(
        node,
        "_server_credentials",
        AsyncMock(return_value=(SimpleNamespace(ssh_user="dev"), "synthetic-key")),
    )
    monkeypatch.setattr(deployer, "_create_deployment_record", AsyncMock(return_value=1))
    monkeypatch.setattr(deployer, "backend_base_url", lambda state: "https://example.test")
    state = {
        "project_id": "pool-project",
        "project_spec": {"slug": "pool-project", "config": {"modules": ["backend"]}},
        "repo_info": {"html_url": "https://github.com/fixture/product"},
        "allocated_resources": {
            "backend": {
                "server_handle": "server",
                "server_ip": "192.0.2.1",
                "port": 8080,
                "service_name": "backend",
            }
        },
        "deploy_fence": fence,
    }
    if outcome in {"cancel", "fence"}:
        with pytest.raises(asyncio.CancelledError if outcome == "cancel" else DeployFenceLost):
            await asyncio.create_task(node._deploy(state))
    else:
        result = await node._deploy(state)
        expected = {
            "success": "success",
            "cancelled": "cancelled",
            "refused": "failed",
            "error": "error",
        }
        assert result["deployment_result"]["status"] == expected[outcome]
    assert len(clients) == 1
    assert clients[0].is_closed
    assert github._http_client is None
    assert requests == (
        []
        if outcome == "cancelled"
        else ["/workflow", "/secrets"] + (["/dispatch"] if outcome == "success" else [])
    )
    assert guards == ([] if outcome == "cancelled" else ["write"])
