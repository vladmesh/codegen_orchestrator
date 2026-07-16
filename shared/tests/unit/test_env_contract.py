"""Tests for the versioned environment contract."""

import json

from pydantic import ValidationError
import pytest

from shared.contracts.env_contract import (
    ENV_CONTRACT_VERSION,
    EnvContractFragment,
    EnvContractMergeError,
    merge_env_contract_fragments,
)


def test_valid_fragment_accepts_each_source_type():
    fragment = EnvContractFragment.model_validate(
        {
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
    )

    assert set(fragment.entries) == {
        "STRIPE_SECRET_KEY",
        "DJANGO_SECRET_KEY",
        "BACKEND_PORT",
        "APP_NAME",
        "POSTGRES_HOST_PORT",
    }


@pytest.mark.parametrize(
    "entry, error",
    [
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
    ],
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
    second = merge_env_contract_fragments(fragments).to_json_bytes()

    assert first == second
    assert list(json.loads(first)["entries"]) == ["BACKEND_PORT", "PUBLIC_API_URL"]
