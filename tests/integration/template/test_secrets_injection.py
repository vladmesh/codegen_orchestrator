"""Tests for secrets injection compatibility."""

from pathlib import Path
import re


class TestSecretsInjection:
    """Test that generated workflows use secrets compatible with orchestrator."""

    def test_workflow_secrets_are_documented(self, generated_backend_project: Path) -> None:
        """Test that all secrets used in workflow are standard.

        Verifies that the workflow uses GitHub's standard secrets
        (like GITHUB_TOKEN) and documented custom secrets.
        """
        workflow_path = generated_backend_project / ".github" / "workflows" / "main.yml"

        with open(workflow_path) as f:
            content = f.read()

        # Extract all secrets references
        # Pattern: ${{ secrets.SECRET_NAME }}
        # We need to handle both raw and {{ }} patterns
        pattern = r"\$\{\{\s*secrets\.([A-Z_0-9]+)\s*\}\}"
        secrets_used = set(re.findall(pattern, content))

        # These are the expected secrets for a backend project
        # Based on the main.yml.jinja template
        expected_secrets = {
            "GITHUB_TOKEN",  # Standard GitHub secret
            "DEPLOY_HOST",  # Deploy target
            "DEPLOY_USER",  # SSH user
            "DEPLOY_SSH_KEY",  # SSH key
            "DEPLOY_PORT",  # SSH port (optional)
            "DEPLOY_PROJECT_PATH",  # Remote project path
            "APP_SECRET_KEY",  # Application secret
            "POSTGRES_PASSWORD",  # DB password
            "REDIS_URL",  # Redis URL (optional)
            "EXTRA_ENV_VARS",  # Extra env vars (optional)
            "DEPLOY_COMPOSE_FILES",  # Compose files (optional)
        }

        # All secrets used should be in our expected set
        unknown_secrets = secrets_used - expected_secrets
        assert not unknown_secrets, f"Unknown secrets found in workflow: {unknown_secrets}"

    def test_core_deploy_secrets_are_used(self, generated_backend_project: Path) -> None:
        """Test that core deployment secrets are referenced.

        These secrets must be present for deploy to work.
        """
        workflow_path = generated_backend_project / ".github" / "workflows" / "main.yml"

        with open(workflow_path) as f:
            content = f.read()

        required_for_deploy = [
            "DEPLOY_HOST",
            "DEPLOY_USER",
            "DEPLOY_SSH_KEY",
            "DEPLOY_PROJECT_PATH",
        ]

        for secret in required_for_deploy:
            assert f"secrets.{secret}" in content, f"Required secret {secret} not found in workflow"

    def test_multi_module_project_has_telegram_secret(
        self, generated_multi_module_project: Path
    ) -> None:
        """Test that multi-module project with tg_bot has telegram secret.

        When tg_bot module is included, TELEGRAM_BOT_TOKEN should be used.
        """
        workflow_path = generated_multi_module_project / ".github" / "workflows" / "main.yml"

        with open(workflow_path) as f:
            content = f.read()

        # Telegram bot token should be used
        assert (
            "TELEGRAM_BOT_TOKEN" in content
        ), "TELEGRAM_BOT_TOKEN should be present for tg_bot module"
