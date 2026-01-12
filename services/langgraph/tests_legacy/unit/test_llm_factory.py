"""Unit tests for LLMFactory."""

import os
from unittest.mock import patch

from langchain_openai import ChatOpenAI
import pytest

from src.llm.factory import LLMFactory

# Test constants
EXPECTED_TEMP_HALF = 0.5
EXPECTED_TEMP_LOW = 0.3


class TestLLMFactory:
    """Tests for LLMFactory class."""

    def test_create_openrouter_llm(self):
        """Test OpenRouter LLM creation with full config."""
        config = {
            "llm_provider": "openrouter",
            "model_identifier": "anthropic/claude-3.5-sonnet",
            "temperature": EXPECTED_TEMP_HALF,
            "openrouter_site_url": "https://example.com",
            "openrouter_app_name": "Test App",
        }

        with patch.dict(os.environ, {"OPEN_ROUTER_KEY": "test-key"}):
            llm = LLMFactory.create_llm(config)

        assert isinstance(llm, ChatOpenAI)
        assert llm.model_name == "anthropic/claude-3.5-sonnet"
        assert llm.temperature == EXPECTED_TEMP_HALF
        # Verify OpenRouter base URL
        assert "openrouter.ai" in str(llm.openai_api_base or llm.base_url or "")

    def test_create_openrouter_llm_minimal_config(self):
        """Test OpenRouter LLM with minimal configuration."""
        config = {
            "llm_provider": "openrouter",
            "model_identifier": "openai/gpt-4o",
            "temperature": 0.0,
        }

        with patch.dict(os.environ, {"OPEN_ROUTER_KEY": "test-key"}):
            llm = LLMFactory.create_llm(config)

        assert isinstance(llm, ChatOpenAI)
        assert llm.model_name == "openai/gpt-4o"
        assert llm.temperature == 0.0  # Default

    def test_create_openai_llm(self):
        """Test direct OpenAI LLM creation."""
        config = {
            "llm_provider": "openai",
            "model_identifier": "gpt-4o-mini",
            "temperature": EXPECTED_TEMP_LOW,
        }

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai-key"}):
            llm = LLMFactory.create_llm(config)

        assert isinstance(llm, ChatOpenAI)
        assert llm.model_name == "gpt-4o-mini"
        assert llm.temperature == EXPECTED_TEMP_LOW

    def test_default_provider_is_openrouter(self):
        """Test that missing provider raises error."""
        config = {
            "model_identifier": "openai/gpt-4o",
            "temperature": 0.0,
        }

        with pytest.raises(KeyError, match="llm_provider"):
            LLMFactory.create_llm(config)

    def test_unknown_provider_raises_error(self):
        """Test that unknown provider raises ValueError."""
        config = {
            "llm_provider": "unknown_provider",
            "model_identifier": "some-model",
            "temperature": EXPECTED_TEMP_LOW,
        }

        with pytest.raises(ValueError, match="Unknown LLM provider"):
            LLMFactory.create_llm(config)

    def test_missing_openrouter_key_raises_error(self):
        """Test that missing OPEN_ROUTER_KEY raises KeyError."""
        config = {
            "llm_provider": "openrouter",
            "model_identifier": "openai/gpt-4o",
            "temperature": EXPECTED_TEMP_LOW,
        }

        # Ensure key is not set
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError, match="OPEN_ROUTER_KEY"):
                LLMFactory.create_llm(config)

    def test_missing_openai_key_raises_error(self):
        """Test that missing OPENAI_API_KEY raises KeyError."""
        config = {
            "llm_provider": "openai",
            "model_identifier": "gpt-4o",
            "temperature": EXPECTED_TEMP_LOW,
        }

        # Ensure key is not set
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError, match="OPENAI_API_KEY"):
                LLMFactory.create_llm(config)

    def test_temperature_defaults_to_zero(self):
        """Test that missing temperature raises KeyError."""
        config = {
            "llm_provider": "openrouter",
            "model_identifier": "openai/gpt-4o",
            # No temperature specified
        }

        with patch.dict(os.environ, {"OPEN_ROUTER_KEY": "test-key"}):
            with pytest.raises(KeyError, match="temperature"):
                LLMFactory.create_llm(config)
