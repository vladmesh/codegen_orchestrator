"""Typed environment-contract resolution at the deploy boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.subgraphs.devops.env_analyzer import env_analyzer_run
from src.subgraphs.devops.graph import resolve_secrets
from src.subgraphs.devops.secret_resolver import SecretResolverNode, TypedSecretResolutionError


def _state(entries: dict, resources: dict | None = None, secrets: dict | None = None) -> dict:
    return {
        "project_id": "project-1",
        "project_spec": {"name": "Test Project", "config": {"secrets": secrets or {}}},
        "provided_secrets": {"USER_TOKEN": "provided-token"},
        "allocated_resources": resources or {},
        "repo_info": {"html_url": "https://github.com/org/repo"},
        "environment_contract": {"entries": entries},
    }


@pytest.mark.asyncio
@patch("src.subgraphs.devops.secret_resolver.api_client")
@patch("src.subgraphs.devops.secret_resolver.decrypt_dict", return_value={})
async def test_contract_resolves_every_source_without_mixing_persisted_maps(_decrypt, api_client):
    api_client.merge_secrets = AsyncMock()
    entries = {
        "USER_TOKEN": {
            "source": "user_secret",
            "environments": ["production"],
            "consumers": ["api"],
            "required": True,
            "description": "User token",
        },
        "APP_SECRET_KEY": {
            "source": "generated_secret",
            "environments": ["production"],
            "required": True,
        },
        "BACKEND_PORT": {
            "source": "allocation",
            "environments": ["production"],
            "required": True,
            "service": "backend",
        },
        "APP_ENV": {
            "source": "derived",
            "environments": ["production"],
            "required": True,
        },
        "DEBUG": {
            "source": "literal",
            "environments": ["production"],
            "required": True,
            "value": False,
        },
    }

    result = await SecretResolverNode().run(
        _state(
            entries, {"backend": {"service_name": "backend", "server_ip": "10.0.0.1", "port": 8000}}
        )
    )

    assert result["secret_values"]["USER_TOKEN"] == "provided-token"  # noqa: S105
    assert "APP_SECRET_KEY" in result["secret_values"]
    assert result["non_secret_values"] == {
        "BACKEND_PORT": "8000",
        "APP_ENV": "production",
        "DEBUG": "False",
    }
    persisted = api_client.merge_secrets.call_args.args[1]
    assert set(persisted) == {"APP_SECRET_KEY"}


@pytest.mark.asyncio
async def test_contract_missing_user_secret_is_a_typed_waiting_outcome():
    entries = {
        "MISSING": {
            "source": "user_secret",
            "environments": ["production"],
            "consumers": ["api"],
            "required": True,
            "description": "Missing credential",
        }
    }
    state = _state(entries)
    state["provided_secrets"] = {}

    result = await SecretResolverNode().run(state)

    assert result["missing_user_secrets"] == ["MISSING"]
    assert result["resolution_outcome"] == "waiting_for_user_secret"


@pytest.mark.asyncio
async def test_contract_missing_allocation_has_a_distinct_outcome():
    entries = {
        "BACKEND_PORT": {
            "source": "allocation",
            "environments": ["production"],
            "required": True,
            "service": "backend",
        }
    }

    with pytest.raises(TypedSecretResolutionError, match="Missing allocation") as error:
        await SecretResolverNode().run(_state(entries))

    assert error.value.outcome == "allocation_missing"


@pytest.mark.asyncio
async def test_invalid_contract_is_a_distinct_outcome():
    result = await resolve_secrets(
        {
            "project_id": "project-1",
            "project_spec": {"name": "Test Project"},
            "environment_contract": {"entries": {"BAD": {"source": "unknown"}}},
        }
    )

    assert result["resolution_outcome"] == "environment_contract_invalid"


@pytest.mark.asyncio
async def test_unknown_derived_value_is_a_resolution_failure():
    entries = {
        "UNKNOWN_DERIVED": {
            "source": "derived",
            "environments": ["production"],
            "required": True,
        }
    }

    with pytest.raises(TypedSecretResolutionError) as error:
        await SecretResolverNode().run(_state(entries))

    assert error.value.outcome == "environment_resolution_failed"


@pytest.mark.asyncio
@patch("src.subgraphs.devops.env_analyzer._fetch_env_contract")
@patch("src.subgraphs.devops.env_analyzer.api_client")
async def test_contract_path_does_not_run_legacy_llm_analyzer(api_client, fetch_contract):
    fetch_contract.return_value = {
        "version": "1",
        "entries": {
            "APP_ENV": {
                "source": "derived",
                "environments": ["production"],
                "required": True,
            }
        },
    }
    api_client.get_project = AsyncMock(return_value=SimpleNamespace(name="Test Project"))
    state = {
        "project_id": "project-1",
        "repo_info": {"html_url": "https://github.com/org/repo"},
        "head_sha": "a" * 40,
    }

    with patch("src.subgraphs.devops.env_analyzer._classify_variables_with_llm") as classify:
        result = await env_analyzer_run(state)

    classify.assert_not_called()
    assert result["environment_contract"] == fetch_contract.return_value
