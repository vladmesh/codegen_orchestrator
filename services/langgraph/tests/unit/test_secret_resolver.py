"""Unit tests for SecretResolverNode."""

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
