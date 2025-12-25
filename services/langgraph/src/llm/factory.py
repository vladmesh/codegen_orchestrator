"""LLM Factory for creating LLM instances from agent configuration.

This module provides a centralized factory for instantiating LLM clients
based on database configuration, supporting multiple providers through OpenRouter
or direct connections.
"""

import logging
import os

from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)


class LLMFactory:
    """Factory for creating LLM instances based on provider configuration.

    Supports:
    - OpenRouter (default): Access to 400+ models from various providers
    - OpenAI: Direct connection to OpenAI API (backward compatibility)
    """

    @staticmethod
    def create_llm(config: dict) -> ChatOpenAI:
        """Create an LLM instance from configuration.

        Args:
            config: Agent configuration dict with keys:
                - llm_provider: Provider name (openrouter, openai)
                - model_identifier: Model ID (e.g., "openai/gpt-4o", "anthropic/claude-3.5-sonnet")
                - temperature: Temperature setting (0.0-2.0)
                - openrouter_site_url: Optional site URL for OpenRouter analytics
                - openrouter_app_name: Optional app name for OpenRouter analytics

        Returns:
            Configured ChatOpenAI instance

        Raises:
            ValueError: If unknown provider is specified
            KeyError: If required environment variables are missing

        Examples:
            >>> config = {
            ...     "llm_provider": "openrouter",
            ...     "model_identifier": "anthropic/claude-3.5-sonnet",
            ...     "temperature": 0.7
            ... }
            >>> llm = LLMFactory.create_llm(config)
        """
        provider = config.get("llm_provider", "openrouter")
        model_id = config.get("model_identifier", "openai/gpt-4o")
        temperature = config.get("temperature", 0.0)

        logger.info(f"Creating LLM: provider={provider}, model={model_id}, temp={temperature}")

        if provider == "openrouter":
            return LLMFactory._create_openrouter_llm(config, model_id, temperature)
        elif provider == "openai":
            return LLMFactory._create_openai_llm(model_id, temperature)
        else:
            raise ValueError(
                f"Unknown LLM provider: {provider}. Supported providers: openrouter, openai"
            )

    @staticmethod
    def _create_openrouter_llm(config: dict, model_id: str, temperature: float) -> ChatOpenAI:
        """Create LLM instance for OpenRouter.

        Args:
            config: Full agent configuration
            model_id: Model identifier (e.g., "openai/gpt-4o")
            temperature: Temperature setting

        Returns:
            ChatOpenAI configured for OpenRouter
        """
        api_key = os.environ.get("OPEN_ROUTER_KEY")
        if not api_key:
            raise KeyError(
                "OPEN_ROUTER_KEY environment variable not set. Please set it to use OpenRouter."
            )

        # Build custom headers for OpenRouter
        headers = {}

        # Add site URL for rankings/analytics (optional)
        site_url = config.get("openrouter_site_url")
        if site_url:
            headers["HTTP-Referer"] = site_url

        # Add app name for identification (optional, defaults to generic name)
        app_name = config.get("openrouter_app_name", "Codegen Orchestrator")
        headers["X-Title"] = app_name

        return ChatOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            model=model_id,
            temperature=temperature,
            default_headers=headers if headers else None,
        )

    @staticmethod
    def _create_openai_llm(model_id: str, temperature: float) -> ChatOpenAI:
        """Create LLM instance for direct OpenAI connection.

        Args:
            model_id: Model identifier (e.g., "gpt-4o")
            temperature: Temperature setting

        Returns:
            ChatOpenAI configured for direct OpenAI API
        """
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise KeyError(
                "OPENAI_API_KEY environment variable not set. "
                "Please set it to use direct OpenAI connection."
            )

        return ChatOpenAI(
            api_key=api_key,
            model=model_id,
            temperature=temperature,
        )
