"""Tests for shared.notifications module — lazy config validation."""

import os
from unittest.mock import patch

import pytest

import shared.notifications as notifications_mod

TEST_TOKEN = "test-token-123"  # noqa: S105
TEST_API_URL = "http://api:8000"
DEFAULT_RATE_LIMIT = 10


@pytest.fixture(autouse=True)
def _reset_config():
    """Reset lazy config cache between tests."""
    notifications_mod._config = None
    yield
    notifications_mod._config = None


class TestNotificationConfig:
    """_ensure_config must fail fast when required env vars are missing."""

    def test_missing_telegram_bot_token_raises(self):
        """TELEGRAM_BOT_TOKEN must be set — no silent empty string."""
        env = {"API_BASE_URL": TEST_API_URL}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
                notifications_mod._ensure_config()

    def test_missing_api_base_url_raises(self):
        """API_BASE_URL must be set — no silent empty string."""
        env = {"TELEGRAM_BOT_TOKEN": TEST_TOKEN}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="API_BASE_URL"):
                notifications_mod._ensure_config()

    def test_valid_config_returns_values(self):
        """When all required vars set, returns them correctly."""
        env = {
            "TELEGRAM_BOT_TOKEN": TEST_TOKEN,
            "API_BASE_URL": TEST_API_URL,
            "NOTIFICATION_RATE_LIMIT": "20",
        }
        with patch.dict(os.environ, env, clear=True):
            config = notifications_mod._ensure_config()
            assert config["telegram_token"] == TEST_TOKEN
            assert config["api_url"] == TEST_API_URL
            expected_limit = 20
            assert config["rate_limit"] == expected_limit

    def test_api_url_with_api_suffix_raises(self):
        """API_BASE_URL must not end with /api."""
        env = {
            "TELEGRAM_BOT_TOKEN": TEST_TOKEN,
            "API_BASE_URL": "http://api:8000/api",
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="must not include /api"):
                notifications_mod._ensure_config()

    def test_rate_limit_defaults_to_10(self):
        """NOTIFICATION_RATE_LIMIT is a tuning param — default OK."""
        env = {
            "TELEGRAM_BOT_TOKEN": TEST_TOKEN,
            "API_BASE_URL": TEST_API_URL,
        }
        with patch.dict(os.environ, env, clear=True):
            config = notifications_mod._ensure_config()
            assert config["rate_limit"] == DEFAULT_RATE_LIMIT

    def test_config_is_cached(self):
        """Second call returns cached config without re-reading env."""
        env = {
            "TELEGRAM_BOT_TOKEN": TEST_TOKEN,
            "API_BASE_URL": TEST_API_URL,
        }
        with patch.dict(os.environ, env, clear=True):
            config1 = notifications_mod._ensure_config()

        # Even with cleared env, cached config should be returned
        with patch.dict(os.environ, {}, clear=True):
            config2 = notifications_mod._ensure_config()

        assert config1 is config2

    def test_import_does_not_require_env_vars(self):
        """Importing the module should NOT raise even without env vars."""
        # This test validates lazy init — the module is already imported
        # but _ensure_config is not called at import time
        assert hasattr(notifications_mod, "notify_admins")
        assert hasattr(notifications_mod, "send_telegram_message")
