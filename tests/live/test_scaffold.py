"""Step 4: Scaffold secrets — Issue #3 (registry secrets missing on repo).

Tests that the scaffolder has the required env vars for setting registry
secrets on GitHub repos during scaffold. If these are empty, CI
build-and-push fails with "Must provide --username with --password-stdin".
"""

import pytest


class TestScaffoldEnvVars:
    """Verify scaffolder container has registry env vars set."""

    @pytest.mark.parametrize(
        "var",
        [
            "REGISTRY_USER",
            "REGISTRY_PASSWORD",
            "ORCHESTRATOR_HOSTNAME",
        ],
    )
    def test_scaffolder_has_registry_env(self, compose_exec, var):
        """Scaffolder container must have non-empty registry env vars.

        RED if .env doesn't define these or docker-compose doesn't pass them.
        """
        value = compose_exec("scaffolder", f"printenv {var}")
        assert value, f"{var} is empty in scaffolder container"
