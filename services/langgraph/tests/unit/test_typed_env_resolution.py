"""Typed environment-contract resolution at the deploy boundary."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from shared.contracts.env_contract import merge_env_contract_fragments
from shared.contracts.env_usage import load_env_contract_fragments
from src.subgraphs.devops.env_contract_loader import load_environment_contract
from src.subgraphs.devops.graph import resolve_secrets
from src.subgraphs.devops.secret_resolver import SecretResolverNode, TypedSecretResolutionError

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _template_fixture() -> Path:
    """Return the rendered fixture for the ref the orchestrator deploys with."""
    configs = yaml.safe_load((_REPO_ROOT / "scripts" / "system_configs.yaml").read_text())
    refs = [c["value"] for c in configs if c["key"] == "scheduler.service_template_ref"]
    assert len(refs) == 1, f"expected one pinned service_template_ref, found {refs}"
    return _REPO_ROOT / "shared/tests/fixtures" / f"service-template-{refs[0]}"


def _state(entries: dict, resources: dict | None = None, secrets: dict | None = None) -> dict:
    return {
        "project_id": "project-1",
        "project_spec": {
            "title": "Test Project",
            "slug": "test-project-0000",
            "config": {"secrets": secrets or {}},
        },
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
        "DEBUG": "false",
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

    assert result["missing_user_secrets"] == [
        {"key": "MISSING", "description": "Missing credential"}
    ]
    assert result["resolution_outcome"] == "waiting_for_user_secret"


def _bot_contract() -> dict:
    return {
        "TG_BOT_ALLOWED_TELEGRAM_IDS": {
            "source": "literal",
            "value": "",
            "environments": ["production"],
            "consumers": ["tg_bot"],
            "required": False,
        }
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "audience"),
    [("only_me", "42"), ("public", ""), ("invite", "42"), ("custom", "42,84")],
)
async def test_bot_access_modes_resolve_the_contract_audience(mode, audience):
    state = _state(_bot_contract())
    state["project_spec"]["config"] = {
        "bot_access": {"mode": mode, "allowed_telegram_ids": audience},
        "env_overrides": {"TG_BOT_ALLOWED_TELEGRAM_IDS": audience},
    }

    result = await SecretResolverNode().run(state)

    assert result["non_secret_values"]["TG_BOT_ALLOWED_TELEGRAM_IDS"] == audience


@pytest.mark.asyncio
async def test_private_bot_with_empty_audience_is_rejected():
    state = _state(_bot_contract())
    state["project_spec"]["config"] = {
        "bot_access": {"mode": "only_me", "allowed_telegram_ids": ""},
        "env_overrides": {"TG_BOT_ALLOWED_TELEGRAM_IDS": ""},
    }

    with pytest.raises(
        TypedSecretResolutionError, match="private bot audience contains no Telegram IDs"
    ) as error:
        await SecretResolverNode().run(state)

    assert error.value.outcome == "environment_contract_invalid"


@pytest.mark.asyncio
async def test_private_bot_with_malformed_audience_is_rejected():
    state = _state(_bot_contract())
    state["project_spec"]["config"] = {
        "bot_access": {"mode": "custom", "allowed_telegram_ids": "not-an-id"},
        "env_overrides": {"TG_BOT_ALLOWED_TELEGRAM_IDS": "not-an-id"},
    }

    with pytest.raises(TypedSecretResolutionError, match="contains no Telegram IDs") as error:
        await SecretResolverNode().run(state)

    assert error.value.outcome == "environment_contract_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "configured_audience", "message_audience"),
    [("public", "", "42"), ("only_me", "42", "84")],
)
async def test_deploy_cannot_replace_configured_bot_audience(
    mode, configured_audience, message_audience
):
    state = _state(_bot_contract())
    state["project_spec"]["config"] = {
        "bot_access": {"mode": mode, "allowed_telegram_ids": configured_audience},
        "env_overrides": {"TG_BOT_ALLOWED_TELEGRAM_IDS": configured_audience},
    }
    state["env_overrides"] = {"TG_BOT_ALLOWED_TELEGRAM_IDS": message_audience}

    with pytest.raises(TypedSecretResolutionError, match="cannot override") as error:
        await SecretResolverNode().run(state)

    assert error.value.outcome == "environment_contract_invalid"


@pytest.mark.asyncio
@patch(
    "src.subgraphs.devops.secret_resolver.decrypt_dict", return_value={"ADMIN_TELEGRAM_ID": "42"}
)
async def test_legacy_admin_secret_is_migrated_to_contract_audience(_decrypt):
    state = _state(_bot_contract(), secrets={"ADMIN_TELEGRAM_ID": "encrypted"})

    result = await SecretResolverNode().run(state)

    assert result["non_secret_values"]["TG_BOT_ALLOWED_TELEGRAM_IDS"] == "42"


@pytest.mark.asyncio
@patch(
    "src.subgraphs.devops.secret_resolver.decrypt_dict",
    return_value={"ADMIN_TELEGRAM_ID": "not-an-id"},
)
async def test_malformed_legacy_admin_secret_is_rejected_before_it_can_make_a_bot_public(_decrypt):
    state = _state(_bot_contract(), secrets={"ADMIN_TELEGRAM_ID": "encrypted"})

    with pytest.raises(TypedSecretResolutionError, match="contains no Telegram IDs") as error:
        await SecretResolverNode().run(state)

    assert error.value.outcome == "environment_contract_invalid"


@pytest.mark.asyncio
@patch(
    "src.subgraphs.devops.secret_resolver.decrypt_dict", return_value={"ADMIN_TELEGRAM_ID": "42"}
)
async def test_legacy_admin_secret_requires_a_contract_that_can_preserve_private_access(_decrypt):
    entries = {
        "DEBUG": {
            "source": "literal",
            "value": False,
            "environments": ["production"],
            "required": True,
        }
    }

    with pytest.raises(
        TypedSecretResolutionError, match="cannot preserve ADMIN_TELEGRAM_ID private access"
    ) as error:
        await SecretResolverNode().run(_state(entries, secrets={"ADMIN_TELEGRAM_ID": "encrypted"}))

    assert error.value.outcome == "environment_contract_invalid"


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
            "project_spec": {"title": "Test Project", "slug": "test-project-0000"},
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
@patch("src.subgraphs.devops.env_contract_loader._fetch_env_contract")
async def test_contract_path_loads_typed_contract(fetch_contract):
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
    state = {
        "project_id": "project-1",
        "repo_info": {"html_url": "https://github.com/org/repo"},
        "head_sha": "a" * 40,
    }

    result = await load_environment_contract(state)

    assert result["environment_contract"] == fetch_contract.return_value
    fetch_contract.assert_awaited_once_with("org", "repo", "a" * 40)


@pytest.mark.asyncio
@patch("src.subgraphs.devops.env_contract_loader._fetch_env_contract")
async def test_missing_head_sha_is_a_distinct_outcome_without_branch_fallback(fetch_contract):
    state = {
        "project_id": "project-1",
        "repo_info": {"html_url": "https://github.com/org/repo"},
        "head_sha": "",
    }

    result = await load_environment_contract(state)

    assert result["resolution_outcome"] == "head_sha_missing"
    assert result["errors"] == ["head_sha is required to load the environment contract"]
    fetch_contract.assert_not_awaited()


@pytest.mark.asyncio
@patch("src.subgraphs.devops.env_contract_loader._fetch_env_contract", side_effect=RuntimeError)
async def test_contract_fetch_failure_is_a_resolution_failure(_fetch_contract):
    state = {
        "project_id": "project-1",
        "repo_info": {"html_url": "https://github.com/org/repo"},
        "head_sha": "a" * 40,
    }

    result = await load_environment_contract(state)

    assert result["resolution_outcome"] == "environment_resolution_failed"


@pytest.mark.asyncio
@patch("src.subgraphs.devops.env_contract_loader.GitHubAppClient")
async def test_repository_without_contract_has_an_invalid_contract_outcome(github_class):
    github = AsyncMock()
    github.list_repo_files_recursive.return_value = []

    github_class.return_value = github
    state = {
        "project_id": "project-1",
        "repo_info": {"html_url": "https://github.com/org/repo"},
        "head_sha": "a" * 40,
    }

    result = await load_environment_contract(state)

    assert result["resolution_outcome"] == "environment_contract_invalid"
    assert result["errors"] == ["environment contract is required"]


@pytest.mark.asyncio
async def test_optional_allocation_failure_remains_fail_fast():
    entries = {
        "OPTIONAL_PORT": {
            "source": "allocation",
            "environments": ["production"],
            "required": False,
            "service": "backend",
        }
    }
    resources = {
        "first": {"service_name": "backend", "server_ip": "10.0.0.1", "port": 8000},
        "second": {"service_name": "backend", "server_ip": "10.0.0.1", "port": 8001},
    }

    with pytest.raises(TypedSecretResolutionError) as error:
        await SecretResolverNode().run(_state(entries, resources))

    assert error.value.outcome == "environment_resolution_failed"


@pytest.mark.asyncio
@patch("src.subgraphs.devops.secret_resolver.api_client")
@patch("src.subgraphs.devops.secret_resolver.decrypt_dict", return_value={})
async def test_template_contract_fixture_resolves_production_entries(_decrypt, api_client):
    api_client.merge_secrets = AsyncMock()
    contract = merge_env_contract_fragments(load_env_contract_fragments(_template_fixture()))
    state = _state(
        contract.model_dump(mode="json").get("entries", {}),
        {
            "backend": {"service_name": "backend", "server_ip": "10.0.0.1", "port": 8000},
            "frontend": {"service_name": "frontend", "server_ip": "10.0.0.1", "port": 8080},
        },
    )
    state["provided_secrets"] = {"TELEGRAM_BOT_TOKEN": "token"}  # noqa: S105

    result = await SecretResolverNode().run(state)

    assert result["missing_user_secrets"] == []
    assert result["non_secret_values"]["POSTGRES_DB"] == "db_project_1"
    assert result["non_secret_values"]["POSTGRES_REQUIRE_SSL"] == "false"
