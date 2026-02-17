"""Unit tests for _set_registry_secrets helper."""

from unittest.mock import patch

import pytest


class TestSetRegistrySecrets:
    """Tests for _set_registry_secrets()."""

    ENV_VARS = {
        "ORCHESTRATOR_HOSTNAME": "registry.example.com",
        "REGISTRY_USER": "testuser",
        "REGISTRY_PASSWORD": "testpass",
    }

    @pytest.mark.asyncio
    async def test_happy_path(self, mock_github):
        """All env vars set → secrets set successfully, returns True."""
        from main import _set_registry_secrets

        # Pre-populate repo in mock so set_repository_secrets can store secrets
        mock_github.secrets["myrepo"] = {}

        with patch.dict("os.environ", self.ENV_VARS):
            result = await _set_registry_secrets("myorg", "myrepo")

        assert result is True
        assert mock_github.secrets["myrepo"]["REGISTRY_URL"] == "registry.example.com"
        assert mock_github.secrets["myrepo"]["REGISTRY_USER"] == "testuser"
        assert mock_github.secrets["myrepo"]["REGISTRY_PASSWORD"] == "testpass"  # noqa: S105

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "missing_key",
        ["ORCHESTRATOR_HOSTNAME", "REGISTRY_USER", "REGISTRY_PASSWORD"],
    )
    async def test_missing_env_var_returns_false(self, mock_github, missing_key):
        """Missing any env var → returns False, no API call."""
        from main import _set_registry_secrets

        partial_env = {k: v for k, v in self.ENV_VARS.items() if k != missing_key}
        with patch.dict("os.environ", partial_env, clear=False):
            # Ensure the missing key is actually absent
            with patch.dict("os.environ", {missing_key: ""}, clear=False):
                import os

                os.environ.pop(missing_key, None)

            result = await _set_registry_secrets("myorg", "myrepo")

        assert result is False

    @pytest.mark.asyncio
    async def test_missing_all_env_vars_returns_false(self, mock_github):
        """No env vars set → returns False."""
        from main import _set_registry_secrets

        with patch.dict("os.environ", {}, clear=False):
            for k in self.ENV_VARS:
                import os

                os.environ.pop(k, None)

            result = await _set_registry_secrets("myorg", "myrepo")

        assert result is False

    @pytest.mark.asyncio
    async def test_github_api_failure_returns_false(self, mock_github):
        """GitHub API failure → returns False, logs error."""
        from main import _set_registry_secrets

        mock_github.should_fail = True
        mock_github.fail_exception = RuntimeError("API error")

        with patch.dict("os.environ", self.ENV_VARS):
            result = await _set_registry_secrets("myorg", "myrepo")

        assert result is False
