"""Tests for secrets injection compatibility."""

from pathlib import Path
import re


class TestSecretsInjection:
    """Test that generated workflows use secrets compatible with orchestrator."""

    def test_ci_workflow_secrets_are_documented(self, generated_backend_project: Path) -> None:
        """Test that all secrets used in CI workflow are standard.

        CI workflow uses registry secrets for image push.
        """
        workflow_path = generated_backend_project / ".github" / "workflows" / "ci.yml"

        with open(workflow_path) as f:
            content = f.read()

        pattern = r"\$\{\{\s*secrets\.([A-Z_0-9]+)\s*\}\}"
        secrets_used = set(re.findall(pattern, content))

        expected_secrets = {
            "REGISTRY_URL",
            "REGISTRY_USER",
            "REGISTRY_PASSWORD",
        }

        unknown_secrets = secrets_used - expected_secrets
        assert not unknown_secrets, f"Unknown secrets found in CI workflow: {unknown_secrets}"

    def test_deploy_workflow_secrets_are_documented(self, generated_backend_project: Path) -> None:
        """Test that all secrets used in deploy workflow are standard."""
        workflow_path = generated_backend_project / ".github" / "workflows" / "deploy.yml"

        with open(workflow_path) as f:
            content = f.read()

        pattern = r"\$\{\{\s*secrets\.([A-Z_0-9]+)\s*\}\}"
        secrets_used = set(re.findall(pattern, content))

        expected_secrets = {
            "DEPLOY_HOST",
            "DEPLOY_USER",
            "DEPLOY_SSH_KEY",
            "DEPLOY_PORT",
            "PROJECT_NAME",
            "DOTENV",
            "REGISTRY_URL",
            "REGISTRY_USER",
            "REGISTRY_PASSWORD",
        }

        unknown_secrets = secrets_used - expected_secrets
        assert not unknown_secrets, f"Unknown secrets found in deploy workflow: {unknown_secrets}"

    def test_core_deploy_secrets_are_used(self, generated_backend_project: Path) -> None:
        """Test that core deployment secrets are referenced.

        These secrets must be present for deploy to work.
        """
        workflow_path = generated_backend_project / ".github" / "workflows" / "deploy.yml"

        with open(workflow_path) as f:
            content = f.read()

        required_for_deploy = [
            "DEPLOY_HOST",
            "DEPLOY_USER",
            "DEPLOY_SSH_KEY",
        ]

        for secret in required_for_deploy:
            assert (
                f"secrets.{secret}" in content
            ), f"Required secret {secret} not found in deploy workflow"
