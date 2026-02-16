"""Unit tests for SecretResolverNode."""

from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

import pytest

from src.subgraphs.devops.nodes import SecretResolverNode


class TestSecretResolverComputeSecret:
    """Tests for SecretResolverNode._compute_secret method."""

    def setup_method(self):
        """Create a fresh node instance for each test."""
        self.node = SecretResolverNode()

    def test_compute_image_value_with_repo_url(self):
        """Image variables should generate GHCR URLs from repository URL."""
        project_spec = {
            "name": "reverse-bot",
            "repository_url": "https://github.com/project-factory-org/reverse-bot",
        }
        state = {}

        result = self.node._compute_secret("BACKEND_IMAGE", project_spec, state)
        assert result == "ghcr.io/project-factory-org/reverse-bot-backend:latest"

        result = self.node._compute_secret("TG_BOT_IMAGE", project_spec, state)
        assert result == "ghcr.io/project-factory-org/reverse-bot-tg-bot:latest"

    def test_compute_image_value_with_config_repo_url(self):
        """Image variables should work with config.repository_url as well."""
        project_spec = {
            "name": "my-app",
            "config": {
                "repository_url": "https://github.com/my-org/my-app",
            },
        }
        state = {}

        result = self.node._compute_secret("FRONTEND_IMAGE", project_spec, state)
        assert result == "ghcr.io/my-org/my-app-frontend:latest"

    def test_compute_image_value_without_repo_url(self):
        """Image variables should fallback when no repo URL is available."""
        project_spec = {"name": "orphan-project"}
        state = {}

        result = self.node._compute_secret("BACKEND_IMAGE", project_spec, state)
        assert result == "ghcr.io/unknown/unknown-service:latest"

    def test_compute_app_name(self):
        """APP_NAME should be derived from project name."""
        project_spec = {"name": "My Cool App"}
        state = {}

        result = self.node._compute_secret("APP_NAME", project_spec, state)
        assert result == "my_cool_app"

    def test_compute_app_env(self):
        """APP_ENV should default to production."""
        project_spec = {"name": "test"}
        state = {}

        result = self.node._compute_secret("APP_ENV", project_spec, state)
        assert result == "production"

        result = self.node._compute_secret("ENVIRONMENT", project_spec, state)
        assert result == "production"

    def test_compute_debug(self):
        """DEBUG should be false in production."""
        project_spec = {"name": "test"}
        state = {}

        result = self.node._compute_secret("DEBUG", project_spec, state)
        assert result == "false"

    def test_compute_postgres_settings(self):
        """Postgres settings should have sensible defaults."""
        project_spec = {"name": "test"}
        state = {}

        assert self.node._compute_secret("POSTGRES_HOST", project_spec, state) == "db"
        assert self.node._compute_secret("POSTGRES_PORT", project_spec, state) == "5432"
        assert self.node._compute_secret("POSTGRES_REQUIRE_SSL", project_spec, state) == "false"

    def test_compute_backend_url_with_resources(self):
        """BACKEND_URL should use allocated resources when available."""
        project_spec = {"name": "test"}
        state = {
            "allocated_resources": {
                "backend": {
                    "server_ip": "192.168.1.100",
                    "port": 8080,
                }
            }
        }

        result = self.node._compute_secret("BACKEND_API_URL", project_spec, state)
        assert result == "http://192.168.1.100:8080"

    def test_compute_backend_url_fallback(self):
        """BACKEND_URL should fallback to localhost when no resources."""
        project_spec = {"name": "test"}
        state = {}

        result = self.node._compute_secret("BACKEND_API_URL", project_spec, state)
        assert result == "http://localhost:8000"


class TestSecretResolverEncryption:
    """Tests for encryption integration in SecretResolverNode."""

    def setup_method(self):
        self.node = SecretResolverNode()

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.nodes.api_client")
    @patch("src.subgraphs.devops.nodes.encrypt_dict")
    @patch("src.subgraphs.devops.nodes.decrypt_dict")
    async def test_saves_encrypted_secrets(self, mock_decrypt, mock_encrypt, mock_api):
        """encrypt_dict should be called before PATCH when saving secrets."""
        mock_decrypt.return_value = {}
        mock_encrypt.return_value = {"DB_URL": "gAAAAA-encrypted"}

        mock_api.get_project = AsyncMock(return_value={"config": {"secrets": {}}})
        mock_api.patch = AsyncMock()

        state = {
            "env_analysis": {"DB_URL": "infra"},
            "provided_secrets": {},
            "project_spec": {"name": "test", "config": {"secrets": {}}},
            "project_id": "proj-123",
        }

        await self.node.run(state)

        # encrypt_dict should have been called with the newly generated secrets
        mock_encrypt.assert_called_once()
        saved_config = mock_api.patch.call_args[1]["json"]["config"]
        assert saved_config["secrets"] == {"DB_URL": "gAAAAA-encrypted"}

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.nodes.encrypt_dict")
    @patch("src.subgraphs.devops.nodes.decrypt_dict")
    async def test_decrypts_existing_secrets(self, mock_decrypt, mock_encrypt):
        """decrypt_dict should be called on config_secrets from project_spec."""
        mock_decrypt.return_value = {"EXISTING_KEY": "decrypted-value"}

        state = {
            "env_analysis": {"EXISTING_KEY": "infra"},
            "provided_secrets": {},
            "project_spec": {
                "name": "test",
                "config": {"secrets": {"EXISTING_KEY": "gAAAAA-encrypted"}},
            },
            "project_id": "proj-123",
        }

        result = await self.node.run(state)

        mock_decrypt.assert_called_once_with({"EXISTING_KEY": "gAAAAA-encrypted"})
        # Existing secret should be reused (decrypted)
        assert result["resolved_secrets"]["EXISTING_KEY"] == "decrypted-value"


class TestSecretResolverGroupIntegration:
    """Tests for SecretResolverNode integration with env_groups."""

    def setup_method(self):
        self.node = SecretResolverNode()

    def _extract_password(self, url: str) -> str:
        """Extract password from a database URL."""
        parsed = urlparse(url)
        return parsed.password

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.nodes.api_client")
    @patch("src.subgraphs.devops.nodes.encrypt_dict")
    @patch("src.subgraphs.devops.nodes.decrypt_dict")
    async def test_postgres_password_matches_database_url(
        self, mock_decrypt, mock_encrypt, mock_api
    ):
        """DATABASE_URL and POSTGRES_PASSWORD must share the same password."""
        mock_decrypt.return_value = {}
        mock_encrypt.return_value = {}
        mock_api.get_project = AsyncMock(return_value={"config": {"secrets": {}}})
        mock_api.patch = AsyncMock()

        state = {
            "env_analysis": {
                "DATABASE_URL": "infra",
                "POSTGRES_PASSWORD": "infra",
                "POSTGRES_USER": "infra",
                "POSTGRES_DB": "infra",
            },
            "provided_secrets": {},
            "project_spec": {"name": "test", "config": {"secrets": {}}},
            "project_id": "my-project",
        }

        result = await self.node.run(state)
        secrets = result["resolved_secrets"]

        db_pass = self._extract_password(secrets["DATABASE_URL"])
        assert db_pass == secrets["POSTGRES_PASSWORD"]
        assert secrets["POSTGRES_USER"] == "postgres"
        assert secrets["POSTGRES_DB"] == "db_my_project"

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.nodes.api_client")
    @patch("src.subgraphs.devops.nodes.encrypt_dict")
    @patch("src.subgraphs.devops.nodes.decrypt_dict")
    async def test_async_database_url_coherent(self, mock_decrypt, mock_encrypt, mock_api):
        """ASYNC_DATABASE_URL password must match DATABASE_URL password."""
        mock_decrypt.return_value = {}
        mock_encrypt.return_value = {}
        mock_api.get_project = AsyncMock(return_value={"config": {"secrets": {}}})
        mock_api.patch = AsyncMock()

        state = {
            "env_analysis": {
                "DATABASE_URL": "infra",
                "ASYNC_DATABASE_URL": "infra",
                "POSTGRES_PASSWORD": "infra",
            },
            "provided_secrets": {},
            "project_spec": {"name": "test", "config": {"secrets": {}}},
            "project_id": "proj-1",
        }

        result = await self.node.run(state)
        secrets = result["resolved_secrets"]

        sync_pass = self._extract_password(secrets["DATABASE_URL"])
        async_pass = self._extract_password(secrets["ASYNC_DATABASE_URL"])
        assert sync_pass == async_pass
        assert sync_pass == secrets["POSTGRES_PASSWORD"]
        assert secrets["ASYNC_DATABASE_URL"].startswith("postgresql+asyncpg://")

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.nodes.encrypt_dict")
    @patch("src.subgraphs.devops.nodes.decrypt_dict")
    async def test_cached_secrets_bypass_groups(self, mock_decrypt, mock_encrypt):
        """Secrets already in config_secrets should NOT be regenerated by groups."""
        mock_decrypt.return_value = {
            "DATABASE_URL": "postgresql://postgres:cached_pw@postgres:5432/db_proj",
            "POSTGRES_PASSWORD": "cached_pw",
        }

        state = {
            "env_analysis": {
                "DATABASE_URL": "infra",
                "POSTGRES_PASSWORD": "infra",
            },
            "provided_secrets": {},
            "project_spec": {
                "name": "test",
                "config": {"secrets": {"DATABASE_URL": "enc1", "POSTGRES_PASSWORD": "enc2"}},
            },
            "project_id": "proj-1",
        }

        result = await self.node.run(state)
        secrets = result["resolved_secrets"]

        cached_pw = "cached_pw"
        assert secrets["DATABASE_URL"] == f"postgresql://postgres:{cached_pw}@postgres:5432/db_proj"
        assert secrets["POSTGRES_PASSWORD"] == cached_pw

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.nodes.api_client")
    @patch("src.subgraphs.devops.nodes.encrypt_dict")
    @patch("src.subgraphs.devops.nodes.decrypt_dict")
    async def test_non_grouped_infra_uses_fallback(self, mock_decrypt, mock_encrypt, mock_api):
        """Infra variables not covered by groups should use _generate_infra_secret fallback."""
        mock_decrypt.return_value = {}
        mock_encrypt.return_value = {}
        mock_api.get_project = AsyncMock(return_value={"config": {"secrets": {}}})
        mock_api.patch = AsyncMock()

        state = {
            "env_analysis": {
                "APP_SECRET_KEY": "infra",
                "JWT_SECRET": "infra",
            },
            "provided_secrets": {},
            "project_spec": {"name": "test", "config": {"secrets": {}}},
            "project_id": "proj-1",
        }

        result = await self.node.run(state)
        secrets = result["resolved_secrets"]

        # Both should be non-empty random strings (generated by fallback)
        min_secret_len = 10
        assert len(secrets["APP_SECRET_KEY"]) > min_secret_len
        assert len(secrets["JWT_SECRET"]) > min_secret_len
        # And they should be different from each other
        assert secrets["APP_SECRET_KEY"] != secrets["JWT_SECRET"]
