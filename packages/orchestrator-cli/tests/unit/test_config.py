import os

from orchestrator_cli.config import Config
import pytest


class TestConfig:
    def test_load_from_env(self):
        """Test simple env var loading"""
        os.environ["ORCHESTRATOR_API_URL"] = "http://test-api:8000"
        os.environ["ORCHESTRATOR_REDIS_URL"] = "redis://test-redis:6379"

        config = Config()

        assert config.api_url == "http://test-api:8000"
        assert config.redis_url == "redis://test-redis:6379"

    def test_missing_env(self):
        """Test missing env vars raises error"""
        # Ensure env vars are unset
        if "ORCHESTRATOR_API_URL" in os.environ:
            del os.environ["ORCHESTRATOR_API_URL"]
        if "ORCHESTRATOR_REDIS_URL" in os.environ:
            del os.environ["ORCHESTRATOR_REDIS_URL"]

        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Config()
