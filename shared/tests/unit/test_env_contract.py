"""Tests for the versioned environment contract."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from pydantic import ValidationError
import pytest

from shared.contracts.env_contract import (
    ENV_CONTRACT_VERSION,
    EnvContractFragment,
    EnvContractMergeError,
    export_env_contract_json_schema,
    merge_env_contract_fragments,
    validate_env_contract_fragment,
)

SCHEMA_PATH = Path(__file__).parents[2] / "contracts/schemas/env-contract.schema.json"


def _fragment(entry: dict) -> dict:
    return {
        "version": ENV_CONTRACT_VERSION,
        "owner": "services/backend",
        "entries": {"KEY": entry},
    }


VALID_FRAGMENT = {
    "version": ENV_CONTRACT_VERSION,
    "owner": "services/backend",
    "entries": {
        "STRIPE_SECRET_KEY": {
            "source": "user_secret",
            "environments": ["production"],
            "consumers": ["backend"],
            "required": True,
            "description": "Stripe server API key",
        },
        "DJANGO_SECRET_KEY": {
            "source": "generated_secret",
            "environments": ["production"],
            "consumers": ["backend"],
            "required": True,
            "description": "Django signing key",
        },
        "BACKEND_PORT": {
            "source": "allocation",
            "environments": ["production"],
            "consumers": ["backend"],
            "required": True,
            "service": "backend",
        },
        "APP_NAME": {
            "source": "derived",
            "environments": ["production"],
            "consumers": ["backend"],
            "required": True,
            "description": "Project name",
        },
        "POSTGRES_HOST_PORT": {
            "source": "literal",
            "environments": ["local"],
            "consumers": ["backend"],
            "required": False,
            "value": 5432,
        },
    },
}


INVALID_ENTRY_CASES = [
    ({"source": "infra"}, "source"),
    (
        {
            "source": "user_secret",
            "environments": ["production"],
            "consumers": [],
            "required": True,
            "description": "Credential",
        },
        "consumers",
    ),
    (
        {
            "source": "user_secret",
            "environments": ["production"],
            "consumers": ["backend"],
            "required": True,
        },
        "description",
    ),
    (
        {
            "source": "allocation",
            "environments": ["production"],
            "consumers": ["backend"],
            "required": True,
        },
        "service or resource",
    ),
    (
        {
            "source": "literal",
            "environments": ["local"],
            "consumers": ["backend"],
            "required": False,
            "value": "not-allowed",
            "sensitive": True,
        },
        "sensitive",
    ),
    (
        {
            "source": "derived",
            "environments": ["production"],
            "consumers": ["backend"],
            "required": True,
            "sensitive": True,
        },
        "sensitive",
    ),
]


ALLOCATION_SELECTOR_CASES = [
    (
        {
            "source": "allocation",
            "environments": ["production"],
            "consumers": ["backend"],
            "required": True,
            "service": "backend",
        },
        True,
    ),
    (
        {
            "source": "allocation",
            "environments": ["production"],
            "consumers": ["backend"],
            "required": True,
            "resource": "port",
        },
        True,
    ),
    (
        {
            "source": "allocation",
            "environments": ["production"],
            "consumers": ["backend"],
            "required": True,
            "service": None,
            "resource": "port",
        },
        True,
    ),
    (
        {
            "source": "allocation",
            "environments": ["production"],
            "consumers": ["backend"],
            "required": True,
        },
        False,
    ),
    (
        {
            "source": "allocation",
            "environments": ["production"],
            "consumers": ["backend"],
            "required": True,
            "service": None,
        },
        False,
    ),
    (
        {
            "source": "allocation",
            "environments": ["production"],
            "consumers": ["backend"],
            "required": True,
            "service": "",
        },
        False,
    ),
]


def test_valid_fragment_accepts_each_source_type():
    fragment = EnvContractFragment.model_validate(VALID_FRAGMENT)

    assert set(fragment.entries) == {
        "STRIPE_SECRET_KEY",
        "DJANGO_SECRET_KEY",
        "BACKEND_PORT",
        "APP_NAME",
        "POSTGRES_HOST_PORT",
    }


@pytest.mark.parametrize(
    "entry, error",
    INVALID_ENTRY_CASES,
)
def test_invalid_fragment_entries_fail_validation(entry, error):
    with pytest.raises(ValidationError, match=error):
        EnvContractFragment.model_validate(
            {
                "version": ENV_CONTRACT_VERSION,
                "owner": "services/backend",
                "entries": {"KEY": entry},
            }
        )


def test_merge_rejects_incompatible_duplicate_key():
    backend = {
        "version": ENV_CONTRACT_VERSION,
        "owner": "services/backend",
        "entries": {
            "APP_NAME": {
                "source": "derived",
                "environments": ["production"],
                "consumers": ["backend"],
                "required": True,
            }
        },
    }
    frontend = {
        "version": ENV_CONTRACT_VERSION,
        "owner": "services/frontend",
        "entries": {
            "APP_NAME": {
                "source": "literal",
                "environments": ["local"],
                "consumers": ["frontend"],
                "required": False,
                "value": "frontend",
            }
        },
    }

    with pytest.raises(EnvContractMergeError, match="APP_NAME"):
        merge_env_contract_fragments([backend, frontend])


def test_merge_produces_byte_stable_canonical_artifact():
    fragments = [
        {
            "version": ENV_CONTRACT_VERSION,
            "owner": "services/frontend",
            "entries": {
                "PUBLIC_API_URL": {
                    "source": "derived",
                    "environments": ["production"],
                    "consumers": ["frontend"],
                    "required": True,
                }
            },
        },
        {
            "version": ENV_CONTRACT_VERSION,
            "owner": "services/backend",
            "entries": {
                "BACKEND_PORT": {
                    "source": "allocation",
                    "environments": ["production"],
                    "consumers": ["backend"],
                    "required": True,
                    "service": "backend",
                }
            },
        },
    ]

    first = merge_env_contract_fragments(fragments).to_json_bytes()
    second = merge_env_contract_fragments(list(reversed(fragments))).to_json_bytes()

    assert first == second
    assert list(json.loads(first)["entries"]) == ["BACKEND_PORT", "PUBLIC_API_URL"]


def test_committed_json_schema_is_exported_from_pydantic_model(tmp_path):
    exported_path = tmp_path / "env-contract.schema.json"
    export_env_contract_json_schema(exported_path)

    assert json.loads(SCHEMA_PATH.read_text()) == json.loads(exported_path.read_text())


@pytest.mark.parametrize(
    "fragment, valid",
    [
        (VALID_FRAGMENT, True),
        *[(_fragment(entry), False) for entry, _ in INVALID_ENTRY_CASES],
        *[(_fragment(entry), valid) for entry, valid in ALLOCATION_SELECTOR_CASES],
    ],
)
def test_json_schema_matches_pydantic_validation(fragment, valid):
    schema_validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))

    assert schema_validator.is_valid(fragment) is valid
    if valid:
        EnvContractFragment.model_validate(fragment)
    else:
        with pytest.raises(ValidationError):
            EnvContractFragment.model_validate(fragment)


def test_validate_fragment_revalidates_constructed_model():
    fragment = EnvContractFragment.model_construct(owner="", entries={})

    with pytest.raises(ValidationError, match="owner"):
        validate_env_contract_fragment(fragment)
