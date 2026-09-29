"""The derived self address is the authoritative backend HTTP endpoint."""

from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

from shared.contracts.queues.deploy import DeployOutcome
from src.subgraphs.devops.secret_resolver import (
    SecretResolverNode,
    TypedSecretResolutionError,
    is_computable_derived_key,
)


def public_state(resources, *, sensitive=False):
    return {
        "project_id": "public-project",
        "project_spec": {"slug": "public-project", "config": {}},
        "allocated_resources": resources,
        "environment_contract": {
            "version": "1",
            "entries": {
                "PUBLIC_BASE_URL": {
                    "source": "derived",
                    "required": True,
                    "sensitive": sensitive,
                    "environments": ["production"],
                }
            },
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("192.0.2.42", "http://192.0.2.42:8080"),
        ("2001:db8::42", "http://[2001:db8::42]:8080"),
        ("::ffff:192.0.2.42", "http://[::ffff:c000:22a]:8080"),
    ],
)
async def test_public_url_uses_backend_and_declared_sensitivity(address, expected, reverse):
    from src.subgraphs.devops.env_contract_loader import uncomputable_required_derived_keys

    resources = {
        "bot": {"service_name": "tg_bot", "server_ip": "192.0.2.99", "port": 8081},
        "api": {"service_name": "backend", "server_ip": address, "port": 8080},
    }
    if reverse:
        resources = dict(reversed(list(resources.items())))
    assert is_computable_derived_key("PUBLIC_BASE_URL")
    assert uncomputable_required_derived_keys(public_state(resources)["environment_contract"]) == []
    result = await SecretResolverNode().run(public_state(resources))
    assert result["non_secret_values"]["PUBLIC_BASE_URL"] == expected
    assert "PUBLIC_BASE_URL" not in result["secret_values"]


@pytest.mark.asyncio
async def test_derived_sensitivity_contract_remains_non_secret():
    with pytest.raises(TypedSecretResolutionError) as caught:
        await SecretResolverNode().run(public_state({}, sensitive=True))
    assert caught.value.outcome is DeployOutcome.ENVIRONMENT_CONTRACT_INVALID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resources",
    [
        {},
        None,
        [],
        {"api": {"service_name": "backend", "server_ip": "localhost", "port": 8080}},
        {"api": {"service_name": "backend", "server_ip": "127.0.0.1", "port": 8080}},
        {"api": {"service_name": "backend", "server_ip": "::", "port": 8080}},
        {"api": {"service_name": "backend", "server_ip": "::ffff:127.0.0.1", "port": 8080}},
        {"api": {"service_name": "backend", "server_ip": "::ffff:0.0.0.0", "port": 8080}},
        {"api": {"service_name": "backend", "server_ip": "::ffff:224.0.0.1", "port": 8080}},
        {"api": {"service_name": "backend", "server_ip": "fe80::42%eth0", "port": 8080}},
        {"api": {"service_name": "backend", "server_ip": "not-an-ip", "port": 8080}},
        {"api": {"service_name": "backend", "server_ip": "192.0.2.42", "port": True}},
        {"api": {"service_name": "backend", "server_ip": "192.0.2.42", "port": 65536}},
        {
            name: {"service_name": "backend", "server_ip": "192.0.2.42", "port": 8080}
            for name in ("a", "b")
        },
    ],
)
async def test_public_url_refuses_unusable_authoritative_endpoint_with_key(resources):
    with pytest.raises(TypedSecretResolutionError, match="PUBLIC_BASE_URL") as caught:
        await SecretResolverNode().run(public_state(resources))
    assert caught.value.outcome is DeployOutcome.ENVIRONMENT_RESOLUTION_FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["::ffff:127.0.0.1", "::ffff:0.0.0.0", "::ffff:224.0.0.1"])
async def test_unusable_effective_address_has_no_deploy_http_or_ssh_handoff(address):
    from src.subgraphs.devops.deployer import DeployerNode
    from src.subgraphs.devops.smoke import SmokeTesterNode

    state = public_state(
        {"backend": {"service_name": "backend", "server_ip": address, "port": 8080}}
    )
    state["project_spec"]["config"]["modules"] = ["backend"]
    state["repo_info"] = {"html_url": "https://github.com/fixture/public"}
    state["secret_values"] = {"TOKEN": "effective-address-secret-canary"}
    with (
        patch("src.subgraphs.devops.deployer.GitHubAppClient") as github,
        patch("src.subgraphs.devops.smoke.httpx.AsyncClient") as http,
        patch("src.subgraphs.devops.smoke.SmokeTesterNode._ssh_run", AsyncMock()) as ssh,
        capture_logs() as logs,
    ):
        for result in (await DeployerNode().run(state), await SmokeTesterNode().run(state)):
            assert result["resolution_outcome"] is DeployOutcome.ENVIRONMENT_RESOLUTION_FAILED
            assert "PUBLIC_BASE_URL" in " ".join(result["errors"])
            assert "effective-address-secret-canary" not in str(result) + str(logs)
        github.assert_not_called()
        http.assert_not_called()
        ssh.assert_not_awaited()
