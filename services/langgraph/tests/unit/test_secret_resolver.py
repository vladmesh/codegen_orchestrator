"""Unit tests for SecretResolverNode."""

import os
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

import pytest

from src.subgraphs.devops.nodes import SecretResolverNode


class TestSecretResolverComputeSecret:
    """Tests for SecretResolverNode._compute_secret method."""

    def setup_method(self):
        """Create a fresh node instance for each test."""
        self.node = SecretResolverNode()

    @patch.dict(os.environ, {"ORCHESTRATOR_HOSTNAME": "testhost.example.com"})
    def test_compute_image_value_with_repo_url(self):
        """Image variables should generate registry URLs from repository URL."""
        project_spec = {
            "name": "reverse-bot",
            "repository_url": "https://github.com/project-factory-org/reverse-bot",
        }
        state = {}

        result = self.node._compute_secret("BACKEND_IMAGE", project_spec, state)
        assert result == "testhost.example.com/project-factory-org/reverse-bot-backend:latest"

        result = self.node._compute_secret("TG_BOT_IMAGE", project_spec, state)
        assert result == "testhost.example.com/project-factory-org/reverse-bot-tg-bot:latest"

    @patch.dict(os.environ, {"ORCHESTRATOR_HOSTNAME": "testhost.example.com"})
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
        assert result == "testhost.example.com/my-org/my-app-frontend:latest"

    @patch.dict(os.environ, {"ORCHESTRATOR_HOSTNAME": "testhost.example.com"})
    def test_compute_image_value_without_repo_url(self):
        """Image variables should fallback when no repo URL is available."""
        project_spec = {"name": "orphan-project"}
        state = {}

        result = self.node._compute_secret("BACKEND_IMAGE", project_spec, state)
        assert result == "testhost.example.com/unknown/unknown-service:latest"

    @patch.dict(os.environ, {}, clear=True)
    def test_compute_image_without_hostname_raises(self):
        """Image variables should raise RuntimeError when ORCHESTRATOR_HOSTNAME is not set."""
        project_spec = {
            "name": "test",
            "repository_url": "https://github.com/org/repo",
        }
        state = {}

        with pytest.raises(RuntimeError, match="ORCHESTRATOR_HOSTNAME"):
            self.node._compute_secret("BACKEND_IMAGE", project_spec, state)

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
                "server1:8080": {
                    "server_ip": "192.168.1.100",
                    "port": 8080,
                    "service_name": "backend",
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

    def test_compute_backend_port_with_resources(self):
        """BACKEND_PORT should resolve to allocated port."""
        project_spec = {"name": "test"}
        state = {
            "allocated_resources": {
                "server1:8080": {
                    "server_ip": "192.168.1.100",
                    "port": 8080,
                    "service_name": "backend",
                }
            }
        }

        result = self.node._compute_secret("BACKEND_PORT", project_spec, state)
        assert result == "8080"

    def test_compute_backend_port_fallback(self):
        """BACKEND_PORT should fallback to 8000 when no resources."""
        project_spec = {"name": "test"}
        state = {}

        result = self.node._compute_secret("BACKEND_PORT", project_spec, state)
        assert result == "8000"

    def test_compute_frontend_port_with_resources(self):
        """FRONTEND_PORT should resolve to the frontend allocation."""
        project_spec = {"name": "test"}
        state = {
            "allocated_resources": {
                "server1:8080": {
                    "server_ip": "192.168.1.100",
                    "port": 8080,
                    "service_name": "backend",
                },
                "server1:8081": {
                    "server_ip": "192.168.1.100",
                    "port": 8081,
                    "service_name": "frontend",
                },
            }
        }

        result = self.node._compute_secret("FRONTEND_PORT", project_spec, state)
        assert result == "8081"

    def test_compute_tg_bot_port_with_resources(self):
        """TG_BOT_PORT should resolve to the tg_bot allocation."""
        project_spec = {"name": "test"}
        state = {
            "allocated_resources": {
                "server1:8082": {
                    "server_ip": "192.168.1.100",
                    "port": 8082,
                    "service_name": "tg_bot",
                }
            }
        }

        result = self.node._compute_secret("TG_BOT_PORT", project_spec, state)
        assert result == "8082"


class TestSecretResolverEncryption:
    """Tests for encryption integration in SecretResolverNode."""

    def setup_method(self):
        self.node = SecretResolverNode()

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.nodes.api_client")
    @patch("src.subgraphs.devops.nodes.decrypt_dict")
    async def test_saves_secrets_via_merge_endpoint(self, mock_decrypt, mock_api):
        """Generated secrets should be saved via merge_secrets (atomic, server-side crypto)."""
        mock_decrypt.return_value = {}
        mock_api.merge_secrets = AsyncMock(return_value={"keys": ["DB_URL"]})

        state = {
            "env_analysis": {"DB_URL": "infra"},
            "provided_secrets": {},
            "project_spec": {"name": "test", "config": {"secrets": {}}},
            "project_id": "proj-123",
        }

        await self.node.run(state)

        mock_api.merge_secrets.assert_called_once()
        call_args = mock_api.merge_secrets.call_args
        assert call_args[0][0] == "proj-123"
        assert "DB_URL" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_save_secrets_uses_atomic_merge(self):
        """_save_secrets_to_project delegates to api_client.merge_secrets."""
        with patch("src.subgraphs.devops.nodes.api_client") as mock_api:
            mock_api.merge_secrets = AsyncMock(return_value={"keys": ["OLD_KEY", "NEW_KEY"]})

            await self.node._save_secrets_to_project("proj-123", {"NEW_KEY": "new-plaintext"})

            mock_api.merge_secrets.assert_called_once_with("proj-123", {"NEW_KEY": "new-plaintext"})
            # No GET+PATCH pattern
            mock_api.get_project.assert_not_called()
            mock_api.patch.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.nodes.api_client")
    @patch("src.subgraphs.devops.nodes.decrypt_dict")
    async def test_decrypts_existing_secrets(self, mock_decrypt, mock_api):
        """decrypt_dict should be called on config_secrets from project_spec."""
        mock_decrypt.return_value = {"EXISTING_KEY": "decrypted-value"}
        mock_api.merge_secrets = AsyncMock(return_value={"keys": ["EXISTING_KEY"]})

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
    @patch("src.subgraphs.devops.nodes.decrypt_dict")
    async def test_postgres_password_matches_database_url(self, mock_decrypt, mock_api):
        """DATABASE_URL and POSTGRES_PASSWORD must share the same password."""
        mock_decrypt.return_value = {}
        mock_api.merge_secrets = AsyncMock(return_value={"keys": []})

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
    @patch("src.subgraphs.devops.nodes.decrypt_dict")
    async def test_async_database_url_coherent(self, mock_decrypt, mock_api):
        """ASYNC_DATABASE_URL password must match DATABASE_URL password."""
        mock_decrypt.return_value = {}
        mock_api.merge_secrets = AsyncMock(return_value={"keys": []})

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
    @patch("src.subgraphs.devops.nodes.api_client")
    @patch("src.subgraphs.devops.nodes.decrypt_dict")
    async def test_cached_secrets_bypass_groups(self, mock_decrypt, mock_api):
        """Secrets already in config_secrets should NOT be regenerated by groups."""
        mock_api.merge_secrets = AsyncMock(return_value={"keys": []})
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
    @patch("src.subgraphs.devops.nodes.decrypt_dict")
    async def test_non_grouped_infra_uses_fallback(self, mock_decrypt, mock_api):
        """Infra variables not covered by groups should use _generate_infra_secret fallback."""
        mock_decrypt.return_value = {}
        mock_api.merge_secrets = AsyncMock(return_value={"keys": []})

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
