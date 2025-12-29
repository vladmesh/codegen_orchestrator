"""Tests for DevOps CI secrets setup functionality."""

from unittest.mock import AsyncMock, patch

import pytest

from src.tools.devops_tools import _setup_ci_secrets as setup_ci_secrets


class TestSetupCISecrets:
    """Tests for setup_ci_secrets function."""

    @pytest.mark.asyncio
    async def test_setup_ci_secrets_success(self, tmp_path):
        """Test successful CI secrets setup."""
        # Create a temporary SSH key file
        ssh_key_file = tmp_path / "id_ed25519"
        ssh_key_file.write_text("fake-ssh-private-key-content")

        mock_github_client = AsyncMock()
        mock_github_client.set_repository_secrets.return_value = 5  # All 5 secrets set

        with patch("src.tools.devops_tools.SSH_KEY_PATH", str(ssh_key_file)):
            result = await setup_ci_secrets(
                github_client=mock_github_client,
                owner="test-org",
                repo="test-repo",
                server_ip="192.168.1.100",
                project_name="my_project",
            )

        assert result is True

        # Verify set_repository_secrets was called with correct secrets
        mock_github_client.set_repository_secrets.assert_called_once()
        call_args = mock_github_client.set_repository_secrets.call_args

        # Arguments are positional: (owner, repo, secrets)
        assert call_args[0][0] == "test-org"
        assert call_args[0][1] == "test-repo"

        secrets = call_args[0][2]
        assert secrets["DEPLOY_HOST"] == "192.168.1.100"
        assert secrets["DEPLOY_USER"] == "root"
        assert secrets["DEPLOY_SSH_KEY"] == "fake-ssh-private-key-content"
        assert secrets["DEPLOY_PROJECT_PATH"] == "/opt/services/my_project"
        assert "compose.base.yml" in secrets["DEPLOY_COMPOSE_FILES"]
        assert "compose.prod.yml" in secrets["DEPLOY_COMPOSE_FILES"]

    @pytest.mark.asyncio
    async def test_setup_ci_secrets_ssh_key_not_found(self):
        """Test that setup_ci_secrets returns False when SSH key is missing."""
        mock_github_client = AsyncMock()

        with patch("src.tools.devops_tools.SSH_KEY_PATH", "/nonexistent/path/id_ed25519"):
            result = await setup_ci_secrets(
                github_client=mock_github_client,
                owner="test-org",
                repo="test-repo",
                server_ip="192.168.1.100",
                project_name="my_project",
            )

        assert result is False
        mock_github_client.set_repository_secrets.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_ci_secrets_partial_failure(self, tmp_path):
        """Test that setup_ci_secrets returns False when not all secrets are set."""
        ssh_key_file = tmp_path / "id_ed25519"
        ssh_key_file.write_text("fake-ssh-key")

        mock_github_client = AsyncMock()
        mock_github_client.set_repository_secrets.return_value = 3  # Only 3 of 5 set

        with patch("src.tools.devops_tools.SSH_KEY_PATH", str(ssh_key_file)):
            result = await setup_ci_secrets(
                github_client=mock_github_client,
                owner="test-org",
                repo="test-repo",
                server_ip="192.168.1.100",
                project_name="my_project",
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_setup_ci_secrets_api_exception(self, tmp_path):
        """Test that setup_ci_secrets handles API exceptions."""
        ssh_key_file = tmp_path / "id_ed25519"
        ssh_key_file.write_text("fake-ssh-key")

        mock_github_client = AsyncMock()
        mock_github_client.set_repository_secrets.side_effect = Exception("API error")

        with patch("src.tools.devops_tools.SSH_KEY_PATH", str(ssh_key_file)):
            result = await setup_ci_secrets(
                github_client=mock_github_client,
                owner="test-org",
                repo="test-repo",
                server_ip="192.168.1.100",
                project_name="my_project",
            )

        assert result is False
