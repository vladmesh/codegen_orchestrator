"""Tests for scaffolder config."""

from pydantic import ValidationError
import pytest

from src.config import Settings

VALID = {
    "redis_url": "redis://localhost:6379",
    "API_BASE_URL": "http://api:8000",
    "workspace_base_path": "/data/workspaces",
    "service_template_path": "/data/service-template",
}


def _settings(**overrides):
    """Create Settings without reading .env file."""
    kwargs = {**VALID, **overrides}
    return Settings(_env_file=None, **kwargs)


class TestSettings:
    def test_valid_config(self):
        settings = _settings()
        assert settings.redis_url == "redis://localhost:6379"
        assert settings.api_base_url == "http://api:8000"
        assert settings.workspace_base_path == "/data/workspaces"
        assert settings.service_template_path == "/data/service-template"

    def test_missing_redis_url(self):
        with pytest.raises(ValidationError):
            Settings(
                _env_file=None, API_BASE_URL="x", workspace_base_path="x", service_template_path="x"
            )

    def test_missing_workspace_base_path(self):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, redis_url="x", API_BASE_URL="x", service_template_path="x")

    def test_missing_service_template_path(self):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, redis_url="x", API_BASE_URL="x", workspace_base_path="x")

    def test_service_template_path(self):
        settings = _settings()
        assert settings.service_template_path == "/data/service-template"
