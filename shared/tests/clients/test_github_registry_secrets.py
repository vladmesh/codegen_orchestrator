"""The product repository's registry secrets, written from the orchestrator's env."""

import base64
from datetime import UTC, datetime, timedelta
import json
import os
from unittest.mock import patch

import httpx
from nacl import public
import pytest
import respx
from structlog.testing import capture_logs

from shared.clients.github import (
    GitHubAppClient,
    RegistrySecretsNotRefreshedError,
    RegistrySecretsRefusal,
    registry_repository_secrets,
)

OWNER, REPO = "my-org", "imported-repo"
REGISTRY_ENV = {
    "ORCHESTRATOR_HOSTNAME": "registry.current.example.com",
    "REGISTRY_USER": "registry-user",
    "REGISTRY_PASSWORD": "registry-password-value",  # noqa: S105
}


@pytest.fixture
def client():
    with patch.dict(
        os.environ, {"GITHUB_APP_ID": "12345", "GITHUB_APP_PRIVATE_KEY_PATH": "dummy.pem"}
    ):
        github = GitHubAppClient()
    github._private_key = "dummy_private_key"
    github._token_cache[111] = ("token", datetime.now(UTC) + timedelta(hours=1))
    with patch.object(github, "get_installation_id", return_value=111):
        yield github


@pytest.fixture
def repository_key():
    """The repository's Actions public key, and the private half that opens what it seals."""
    private = public.PrivateKey.generate()
    return private, base64.b64encode(bytes(private.public_key)).decode()


def _sealed_values(routes, private_key) -> dict[str, str]:
    box = public.SealedBox(private_key)
    written = {}
    for name, route in routes.items():
        for call in route.calls:
            body = json.loads(call.request.content)
            written[name] = box.decrypt(base64.b64decode(body["encrypted_value"])).decode()
    return written


def test_the_secrets_are_named_for_the_product_ci_and_valued_from_the_env():
    assert registry_repository_secrets(REGISTRY_ENV) == {
        "REGISTRY_URL": "registry.current.example.com",
        "REGISTRY_USER": "registry-user",
        "REGISTRY_PASSWORD": "registry-password-value",
    }


def test_a_missing_variable_is_named_and_no_value_is():
    env = {**REGISTRY_ENV, "REGISTRY_USER": "", "ORCHESTRATOR_HOSTNAME": ""}
    del env["REGISTRY_PASSWORD"]

    with pytest.raises(RegistrySecretsNotRefreshedError) as refused:
        registry_repository_secrets(env)

    assert refused.value.reason is RegistrySecretsRefusal.ENV_MISSING
    assert refused.value.detail == "ORCHESTRATOR_HOSTNAME, REGISTRY_USER, REGISTRY_PASSWORD not set"


@pytest.mark.asyncio
async def test_refresh_writes_the_current_values_over_stale_ones(client, repository_key):
    private_key, public_key_b64 = repository_key
    with patch.dict(os.environ, REGISTRY_ENV), respx.mock(base_url="https://api.github.com") as gh:
        gh.get(f"/repos/{OWNER}/{REPO}/actions/secrets/public-key").mock(
            return_value=httpx.Response(200, json={"key": public_key_b64, "key_id": "k1"})
        )
        routes = {
            name: gh.put(f"/repos/{OWNER}/{REPO}/actions/secrets/{name}").mock(
                return_value=httpx.Response(204)
            )
            for name in ("REGISTRY_URL", "REGISTRY_USER", "REGISTRY_PASSWORD")
        }
        with capture_logs() as logs:
            await client.refresh_registry_secrets(OWNER, REPO)

    assert _sealed_values(routes, private_key) == {
        "REGISTRY_URL": "registry.current.example.com",
        "REGISTRY_USER": "registry-user",
        "REGISTRY_PASSWORD": "registry-password-value",
    }
    assert any(entry["event"] == "registry_secrets_refreshed" for entry in logs)
    assert "registry-password-value" not in repr(logs)


@pytest.mark.asyncio
async def test_an_incomplete_write_refuses_and_logs_no_value(client, repository_key):
    _private_key, public_key_b64 = repository_key
    with patch.dict(os.environ, REGISTRY_ENV), respx.mock(base_url="https://api.github.com") as gh:
        gh.get(f"/repos/{OWNER}/{REPO}/actions/secrets/public-key").mock(
            return_value=httpx.Response(200, json={"key": public_key_b64, "key_id": "k1"})
        )
        for name in ("REGISTRY_URL", "REGISTRY_USER"):
            gh.put(f"/repos/{OWNER}/{REPO}/actions/secrets/{name}").mock(
                return_value=httpx.Response(204)
            )
        # A failure whose own text carries the value it was writing.
        gh.put(f"/repos/{OWNER}/{REPO}/actions/secrets/REGISTRY_PASSWORD").mock(
            side_effect=ValueError("could not write registry-password-value")
        )
        with capture_logs() as logs, pytest.raises(RegistrySecretsNotRefreshedError) as refused:
            await client.refresh_registry_secrets(OWNER, REPO)

    assert refused.value.reason is RegistrySecretsRefusal.WRITE_INCOMPLETE
    assert refused.value.detail == f"wrote 2 of 3 registry secrets to {OWNER}/{REPO}"
    failed = [entry for entry in logs if entry["event"] == "github_secret_set_failed"]
    assert [entry["secret_name"] for entry in failed] == ["REGISTRY_PASSWORD"]
    assert "registry-password-value" not in repr(logs)


@pytest.mark.asyncio
async def test_a_missing_variable_writes_nothing(client):
    env = {**REGISTRY_ENV, "REGISTRY_PASSWORD": ""}
    with patch.dict(os.environ, env), respx.mock(base_url="https://api.github.com") as gh:
        with pytest.raises(RegistrySecretsNotRefreshedError) as refused:
            await client.refresh_registry_secrets(OWNER, REPO)

    assert refused.value.reason is RegistrySecretsRefusal.ENV_MISSING
    assert not gh.calls
