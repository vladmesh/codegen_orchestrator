"""The product repository's registry secrets and the writer policy, read from the env."""

import os
from unittest.mock import patch

from cryptography.fernet import Fernet
from pydantic import ValidationError
import pytest

from shared.clients.github import (
    RegistrySecretsNotRefreshedError,
    RegistrySecretsRefusal,
    registry_repository_secrets,
    registry_writer_lease_seconds,
)

LEASE_POLICY = "GITHUB_REGISTRY_SECRETS_LEASE_SECONDS"

REGISTRY_ENV = {
    "ORCHESTRATOR_HOSTNAME": "registry.current.example.com",
    "REGISTRY_USER": "registry-user",
    "REGISTRY_PASSWORD": "registry-password-value",  # noqa: S105
}


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


@pytest.mark.parametrize("value", [None, "", "0", "-5", "soon", "1.5"])
def test_a_missing_or_non_positive_lease_policy_refuses_the_writer(value):
    env = {"SECRETS_ENCRYPTION_KEY": Fernet.generate_key().decode()}
    with patch.dict(os.environ, env):
        os.environ.pop(LEASE_POLICY, None)
        if value is not None:
            os.environ[LEASE_POLICY] = value
        with pytest.raises(ValidationError, match="github_registry_secrets_lease_seconds"):
            registry_writer_lease_seconds()


def test_the_lease_policy_is_whole_seconds():
    env = {LEASE_POLICY: "180", "SECRETS_ENCRYPTION_KEY": Fernet.generate_key().decode()}
    with patch.dict(os.environ, env):
        assert registry_writer_lease_seconds() == 180


@pytest.mark.parametrize("key", ["", "not-a-fernet-key"], ids=["missing", "invalid"])
def test_an_unusable_receipt_key_refuses_the_writer(key):
    with patch.dict(os.environ, {LEASE_POLICY: "180", "SECRETS_ENCRYPTION_KEY": key}):
        with pytest.raises(RegistrySecretsNotRefreshedError) as refused:
            registry_writer_lease_seconds()

    assert refused.value.reason is RegistrySecretsRefusal.RECEIPT_KEY_INVALID
