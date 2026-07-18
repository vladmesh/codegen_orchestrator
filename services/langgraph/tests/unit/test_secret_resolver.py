"""Unit tests for SecretResolverNode."""

import os
from unittest.mock import patch

import pytest

from src.subgraphs.devops.secret_resolver import SecretResolutionError, SecretResolverNode


class TestSecretResolverComputeSecret:
    """Tests for SecretResolverNode._compute_secret method."""

    def setup_method(self):
        """Create a fresh node instance for each test."""
        self.node = SecretResolverNode()

    @patch.dict(os.environ, {"ORCHESTRATOR_HOSTNAME": "testhost.example.com"})
    def test_compute_image_value_with_repo_info(self):
        """Image variables should generate registry URLs from repo_info in state."""
        project_spec = {"title": "Reverse Bot", "slug": "reverse-bot-0000"}
        state = {
            "repo_info": {
                "html_url": "https://github.com/project-factory-org/reverse-bot",
            }
        }

        result = self.node._compute_secret("BACKEND_IMAGE", project_spec, state)
        assert result == "testhost.example.com/project-factory-org/reverse-bot-backend:latest"

        result = self.node._compute_secret("TG_BOT_IMAGE", project_spec, state)
        assert result == "testhost.example.com/project-factory-org/reverse-bot-tg-bot:latest"

    @patch.dict(os.environ, {"ORCHESTRATOR_HOSTNAME": "testhost.example.com"})
    def test_compute_image_value_with_different_repo_info(self):
        """Image variables should work with repo_info html_url."""
        project_spec = {"title": "My App", "slug": "my-app-0000"}
        state = {
            "repo_info": {
                "html_url": "https://github.com/my-org/my-app",
            }
        }

        result = self.node._compute_secret("FRONTEND_IMAGE", project_spec, state)
        assert result == "testhost.example.com/my-org/my-app-frontend:latest"

    @patch.dict(os.environ, {"ORCHESTRATOR_HOSTNAME": "testhost.example.com"})
    def test_compute_image_without_repo_info_raises(self):
        """Image variables require complete repository metadata."""
        project_spec = {"title": "Orphan Project", "slug": "orphan-project-0000"}
        state = {}

        with pytest.raises(SecretResolutionError, match="repository metadata"):
            self.node._compute_secret("BACKEND_IMAGE", project_spec, state)

    @patch.dict(os.environ, {"ORCHESTRATOR_HOSTNAME": "testhost.example.com"})
    def test_compute_image_with_malformed_repo_info_raises(self):
        """A partial repository URL cannot become a Docker image name."""
        state = {"repo_info": {"html_url": "https://github.com/project-factory-org"}}

        with pytest.raises(SecretResolutionError, match="repository metadata is malformed"):
            self.node._compute_secret("BACKEND_IMAGE", {"slug": "test-0000"}, state)

    @patch.dict(os.environ, {}, clear=True)
    def test_compute_image_without_hostname_raises(self):
        """Image variables should raise RuntimeError when ORCHESTRATOR_HOSTNAME is not set."""
        project_spec = {"slug": "test-0000"}
        state = {
            "repo_info": {"html_url": "https://github.com/org/repo"},
        }

        with pytest.raises(RuntimeError, match="ORCHESTRATOR_HOSTNAME"):
            self.node._compute_secret("BACKEND_IMAGE", project_spec, state)

    def test_runtime_name_values_use_project_slug(self):
        """Runtime names come from slug, not display title."""
        project_spec = {"title": "My Cool App", "slug": "my-cool-app-0000"}
        state = {}

        assert self.node._compute_secret("APP_NAME", project_spec, state) == "my-cool-app-0000"
        assert self.node._compute_secret("PROJECT_NAME", project_spec, state) == "my-cool-app-0000"
        assert (
            self.node._compute_secret("COMPOSE_PROJECT_NAME", project_spec, state)
            == "my-cool-app-0000"
        )

    def test_compute_app_env(self):
        """APP_ENV should default to production."""
        project_spec = {"slug": "test-0000"}
        state = {}

        result = self.node._compute_secret("APP_ENV", project_spec, state)
        assert result == "production"

        result = self.node._compute_secret("ENVIRONMENT", project_spec, state)
        assert result == "production"

    def test_compute_debug(self):
        """DEBUG should be false in production."""
        project_spec = {"slug": "test-0000"}
        state = {}

        result = self.node._compute_secret("DEBUG", project_spec, state)
        assert result == "false"

    def test_compute_postgres_settings(self):
        """Postgres settings should have sensible defaults."""
        project_spec = {"slug": "test-0000"}
        state = {}

        assert self.node._compute_secret("POSTGRES_HOST", project_spec, state) == "db"
        assert self.node._compute_secret("POSTGRES_PORT", project_spec, state) == "5432"
        assert self.node._compute_secret("POSTGRES_REQUIRE_SSL", project_spec, state) == "false"

    def test_compute_backend_url_uses_docker_service_name(self):
        """BACKEND_API_URL should use docker service name for inter-service communication."""
        project_spec = {"slug": "test-0000"}
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
        assert result == "http://backend:8000"

    def test_compute_backend_url_without_resources(self):
        """BACKEND_API_URL should use docker service name even without allocated resources."""
        project_spec = {"slug": "test-0000"}
        state = {}

        result = self.node._compute_secret("BACKEND_API_URL", project_spec, state)
        assert result == "http://backend:8000"

    def test_compute_api_url_variants(self):
        """All inter-service URL variants should use docker service name."""
        project_spec = {"slug": "test-0000"}
        state = {}

        for var in ("BACKEND_API_URL", "API_URL", "API_BASE_URL", "BACKEND_URL"):
            result = self.node._compute_secret(var, project_spec, state)
            assert result == "http://backend:8000", f"{var} should be http://backend:8000"

    def test_compute_backend_port_with_resources(self):
        """BACKEND_PORT should resolve to allocated port."""
        project_spec = {"slug": "test-0000"}
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

    def test_compute_backend_port_without_allocation_raises(self):
        """BACKEND_PORT requires a matching allocation."""
        project_spec = {"slug": "test-0000"}
        state = {}

        with pytest.raises(SecretResolutionError, match="allocation"):
            self.node._compute_secret("BACKEND_PORT", project_spec, state)

    def test_compute_frontend_port_with_resources(self):
        """FRONTEND_PORT should resolve to the frontend allocation."""
        project_spec = {"slug": "test-0000"}
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
        project_spec = {"slug": "test-0000"}
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

    def test_compute_database_host_ports_from_allocations(self):
        """Compose host ports use their persisted application allocations."""
        state = {
            "allocated_resources": {
                "server1:18001": {
                    "server_ip": "192.168.1.100",
                    "port": 18001,
                    "service_name": "postgres",
                },
                "server1:18002": {
                    "server_ip": "192.168.1.100",
                    "port": 18002,
                    "service_name": "redis",
                },
            }
        }

        assert (
            self.node._compute_secret("POSTGRES_HOST_PORT", {"slug": "test-0000"}, state) == "18001"
        )
        assert self.node._compute_secret("REDIS_HOST_PORT", {"slug": "test-0000"}, state) == "18002"

    @pytest.mark.parametrize(
        "key,service",
        [("POSTGRES_HOST_PORT", "postgres"), ("REDIS_HOST_PORT", "redis")],
    )
    def test_compute_database_host_port_requires_unambiguous_allocation(self, key, service):
        """Missing and duplicate service allocations fail visibly."""
        with pytest.raises(
            SecretResolutionError, match=f"Missing allocation for service {service}"
        ):
            self.node._compute_secret(key, {"slug": "test-0000"}, {"allocated_resources": {}})

        resources = {
            f"server1:{port}": {
                "server_ip": "192.168.1.100",
                "port": port,
                "service_name": service,
            }
            for port in (18001, 18002)
        }
        with pytest.raises(
            SecretResolutionError, match=f"Ambiguous allocation for service {service}"
        ):
            self.node._compute_secret(
                key, {"slug": "test-0000"}, {"allocated_resources": resources}
            )

    def test_compute_unknown_key_raises(self):
        """Computed keys must have an explicit resolver."""
        with pytest.raises(SecretResolutionError, match="Unknown computed secret"):
            self.node._compute_secret("UNRECOGNIZED_VALUE", {"slug": "test-0000"}, {})

    @pytest.mark.parametrize(
        "allocation",
        [
            {"service_name": "backend", "server_ip": "localhost", "port": 8080},
            {"service_name": "backend", "server_ip": "not-an-ip", "port": 8080},
            {"service_name": "backend", "server_ip": "10.0.0.1", "port": 0},
            {"service_name": "backend", "server_ip": "10.0.0.1", "port": "8080"},
        ],
    )
    def test_compute_port_rejects_malformed_allocation(self, allocation):
        """Port variables do not substitute localhost or a default port."""
        state = {"allocated_resources": {"backend": allocation}}

        with pytest.raises(SecretResolutionError, match="allocation"):
            self.node._compute_secret("BACKEND_PORT", {"slug": "test-0000"}, state)
