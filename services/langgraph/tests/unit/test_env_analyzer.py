"""Unit tests for env_analyzer module."""

from unittest.mock import AsyncMock, patch

import pytest

from src.subgraphs.devops.env_analyzer import (
    _classify_by_pattern,
    _classify_variables_with_llm,
    _fetch_compose_env_context,
    _parse_env_variables,
)


class TestClassifyByPattern:
    """Tests for _classify_by_pattern function."""

    def test_classify_image_as_computed(self):
        """Docker image variables should be classified as computed."""
        assert _classify_by_pattern("BACKEND_IMAGE") == "computed"
        assert _classify_by_pattern("TG_BOT_IMAGE") == "computed"
        assert _classify_by_pattern("FRONTEND_IMAGE") == "computed"
        assert _classify_by_pattern("backend_image") == "computed"

    def test_classify_infra_exact(self):
        """Exact infra matches should be classified as infra."""
        assert _classify_by_pattern("DATABASE_URL") == "infra"
        assert _classify_by_pattern("REDIS_URL") == "infra"
        assert _classify_by_pattern("POSTGRES_USER") == "infra"
        assert _classify_by_pattern("POSTGRES_PASSWORD") == "infra"

    def test_classify_infra_patterns(self):
        """Infra pattern matches should be classified as infra."""
        assert _classify_by_pattern("APP_SECRET_KEY") == "infra"
        assert _classify_by_pattern("JWT_SECRET") == "infra"
        assert _classify_by_pattern("SESSION_SECRET") == "infra"

    def test_classify_computed_exact(self):
        """Exact computed matches should be classified as computed."""
        assert _classify_by_pattern("APP_NAME") == "computed"
        assert _classify_by_pattern("APP_ENV") == "computed"
        assert _classify_by_pattern("DEBUG") == "computed"
        assert _classify_by_pattern("BACKEND_URL") == "computed"

    def test_classify_user_patterns(self):
        """User pattern matches should be classified as user."""
        assert _classify_by_pattern("TELEGRAM_BOT_TOKEN") == "user"
        assert _classify_by_pattern("OPENAI_API_KEY") == "user"
        assert _classify_by_pattern("STRIPE_API_KEY") == "user"
        # Note: STRIPE_SECRET_KEY is classified as infra because SECRET_KEY is an infra pattern

    def test_unknown_returns_none(self):
        """Unknown variables should return None."""
        assert _classify_by_pattern("RANDOM_VAR") is None
        assert _classify_by_pattern("UNKNOWN_SETTING") is None


class TestParseEnvVariables:
    """Tests for _parse_env_variables with comment extraction."""

    def test_parse_env_variables_with_comments(self):
        """Comment above a variable is captured."""
        content = """\
# Database connection string
DATABASE_URL=postgres://localhost/db
# Redis cache URL
REDIS_URL=redis://localhost:6379
"""
        result = _parse_env_variables(content)
        assert result == [
            ("DATABASE_URL", "Database connection string"),
            ("REDIS_URL", "Redis cache URL"),
        ]

    def test_parse_env_variables_no_comment(self):
        """Variable without preceding comment returns None."""
        content = """\
DATABASE_URL=postgres://localhost/db
REDIS_URL=redis://localhost:6379
"""
        result = _parse_env_variables(content)
        assert result == [
            ("DATABASE_URL", None),
            ("REDIS_URL", None),
        ]

    def test_parse_env_variables_blank_line_resets_comment(self):
        """Blank line between comment and variable breaks association."""
        content = """\
# This comment is orphaned

DATABASE_URL=postgres://localhost/db
"""
        result = _parse_env_variables(content)
        assert result == [("DATABASE_URL", None)]

    def test_parse_env_variables_inline_format(self):
        """Various # prefixes are handled."""
        content = """\
# Standard comment
VAR_A=1
## Double hash
VAR_B=2
#No space after hash
VAR_C=3
"""
        result = _parse_env_variables(content)
        assert result[0] == ("VAR_A", "Standard comment")
        assert result[1] == ("VAR_B", "Double hash")
        assert result[2] == ("VAR_C", "No space after hash")

    def test_parse_env_variables_mixed(self):
        """Mix of commented and uncommented variables."""
        content = """\
# API key for notifications
NOTIFICATION_KEY=abc
MAX_RETRIES=3
# Analytics toggle
ENABLE_ANALYTICS=true
"""
        result = _parse_env_variables(content)
        assert result == [
            ("NOTIFICATION_KEY", "API key for notifications"),
            ("MAX_RETRIES", None),
            ("ENABLE_ANALYTICS", "Analytics toggle"),
        ]

    def test_parse_env_variables_comment_resets_after_var(self):
        """Comment is consumed by the first variable after it."""
        content = """\
# Only applies to VAR_A
VAR_A=1
VAR_B=2
"""
        result = _parse_env_variables(content)
        assert result == [
            ("VAR_A", "Only applies to VAR_A"),
            ("VAR_B", None),
        ]


class TestFetchComposeEnvContext:
    """Tests for _fetch_compose_env_context."""

    @pytest.mark.asyncio
    async def test_parses_services(self):
        """Extracts env vars per service from compose YAML."""
        compose_yaml = """\
services:
  backend:
    image: backend:latest
    environment:
      DATABASE_URL: postgres://db/app
      REDIS_URL: redis://redis:6379
  worker:
    image: worker:latest
    environment:
      - REDIS_URL=redis://redis:6379
      - TASK_QUEUE=default
"""
        with patch("src.subgraphs.devops.env_analyzer.GitHubAppClient") as mock_cls:
            mock_github = AsyncMock()
            mock_github.get_file_contents.return_value = compose_yaml
            mock_cls.return_value = mock_github

            result = await _fetch_compose_env_context("owner", "repo")

        assert result is not None
        assert "Service 'backend' uses: DATABASE_URL, REDIS_URL" in result
        assert "Service 'worker' uses: REDIS_URL, TASK_QUEUE" in result

    @pytest.mark.asyncio
    async def test_handles_list_and_dict_formats(self):
        """Both list and dict environment formats are parsed."""
        compose_yaml = """\
services:
  svc_list:
    environment:
      - VAR_A
      - VAR_B=value
  svc_dict:
    environment:
      VAR_C: value
      VAR_D: other
"""
        with patch("src.subgraphs.devops.env_analyzer.GitHubAppClient") as mock_cls:
            mock_github = AsyncMock()
            mock_github.get_file_contents.return_value = compose_yaml
            mock_cls.return_value = mock_github

            result = await _fetch_compose_env_context("owner", "repo")

        assert "Service 'svc_dict' uses: VAR_C, VAR_D" in result
        assert "Service 'svc_list' uses: VAR_A, VAR_B" in result

    @pytest.mark.asyncio
    async def test_file_not_found(self):
        """Returns None when compose file doesn't exist."""
        with patch("src.subgraphs.devops.env_analyzer.GitHubAppClient") as mock_cls:
            mock_github = AsyncMock()
            mock_github.get_file_contents.return_value = None
            mock_cls.return_value = mock_github

            result = await _fetch_compose_env_context("owner", "repo")

        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_yaml(self):
        """Returns None on invalid YAML."""
        with patch("src.subgraphs.devops.env_analyzer.GitHubAppClient") as mock_cls:
            mock_github = AsyncMock()
            mock_github.get_file_contents.return_value = "{{invalid yaml::"
            mock_cls.return_value = mock_github

            result = await _fetch_compose_env_context("owner", "repo")

        assert result is None

    @pytest.mark.asyncio
    async def test_no_services_key(self):
        """Returns None when YAML has no services key."""
        with patch("src.subgraphs.devops.env_analyzer.GitHubAppClient") as mock_cls:
            mock_github = AsyncMock()
            mock_github.get_file_contents.return_value = "version: '3'"
            mock_cls.return_value = mock_github

            result = await _fetch_compose_env_context("owner", "repo")

        assert result is None


class TestClassifyVariablesWithLlm:
    """Tests for _classify_variables_with_llm with comments integration."""

    @pytest.mark.asyncio
    async def test_comments_included_in_llm_prompt(self):
        """Verify LLM prompt includes comments for unknown variables."""
        mock_response = AsyncMock()
        mock_response.content = '{"NOTIFICATION_WEBHOOK_URL": "user", "MAX_RETRIES": "computed"}'

        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = mock_response

        mock_config = {"model": "test"}

        with (
            patch("src.subgraphs.devops.env_analyzer.agent_config_cache") as mock_cache,
            patch("src.subgraphs.devops.env_analyzer.LLMFactory") as mock_factory,
        ):
            mock_cache.get = AsyncMock(return_value=mock_config)
            mock_factory.create_llm.return_value = mock_llm

            comments = {
                "NOTIFICATION_WEBHOOK_URL": "Webhook URL for external notifications",
                "MAX_RETRIES": "Maximum retry attempts",
            }

            result, response = await _classify_variables_with_llm(
                ["NOTIFICATION_WEBHOOK_URL", "MAX_RETRIES"],
                "Project: test",
                comments=comments,
            )

        # Verify the prompt sent to LLM contains comments
        call_args = mock_llm.ainvoke.call_args[0][0]
        prompt_text = call_args[0].content
        assert "NOTIFICATION_WEBHOOK_URL  # Webhook URL for external notifications" in prompt_text
        assert "MAX_RETRIES  # Maximum retry attempts" in prompt_text

    @pytest.mark.asyncio
    async def test_compose_context_included_in_llm_prompt(self):
        """Verify compose service info appears in project context."""
        mock_response = AsyncMock()
        mock_response.content = '{"CUSTOM_VAR": "user"}'

        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = mock_response

        mock_config = {"model": "test"}

        with (
            patch("src.subgraphs.devops.env_analyzer.agent_config_cache") as mock_cache,
            patch("src.subgraphs.devops.env_analyzer.LLMFactory") as mock_factory,
        ):
            mock_cache.get = AsyncMock(return_value=mock_config)
            mock_factory.create_llm.return_value = mock_llm

            project_context = """
Project Name: test-project
Repository: https://github.com/org/repo

Docker Compose services:
Service 'backend' uses: CUSTOM_VAR, DATABASE_URL
"""

            result, response = await _classify_variables_with_llm(
                ["CUSTOM_VAR"],
                project_context,
            )

        call_args = mock_llm.ainvoke.call_args[0][0]
        prompt_text = call_args[0].content
        assert "Docker Compose services:" in prompt_text
        assert "Service 'backend' uses: CUSTOM_VAR, DATABASE_URL" in prompt_text

    @pytest.mark.asyncio
    async def test_no_comments_for_vars_without_them(self):
        """Variables without comments are listed without # suffix."""
        mock_response = AsyncMock()
        mock_response.content = '{"ENABLE_ANALYTICS": "computed"}'

        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = mock_response

        mock_config = {"model": "test"}

        with (
            patch("src.subgraphs.devops.env_analyzer.agent_config_cache") as mock_cache,
            patch("src.subgraphs.devops.env_analyzer.LLMFactory") as mock_factory,
        ):
            mock_cache.get = AsyncMock(return_value=mock_config)
            mock_factory.create_llm.return_value = mock_llm

            result, response = await _classify_variables_with_llm(
                ["ENABLE_ANALYTICS"],
                "Project: test",
                comments={},
            )

        call_args = mock_llm.ainvoke.call_args[0][0]
        prompt_text = call_args[0].content
        assert "- ENABLE_ANALYTICS\n" in prompt_text or prompt_text.endswith("- ENABLE_ANALYTICS")
        assert "#" not in prompt_text.split("ENABLE_ANALYTICS")[1].split("\n")[0]
