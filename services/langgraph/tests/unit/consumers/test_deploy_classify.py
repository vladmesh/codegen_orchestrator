"""Unit tests for deploy failure classification (three-way: RETRY / CODE_FIX / GIVE_UP)."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("OPEN_ROUTER_KEY", "test-key")


class TestClassifyDeployFailure:
    """Tests for _classify_deploy_failure()."""

    @pytest.mark.asyncio
    async def test_port_conflict_classified_as_give_up(self):
        """Port conflict is a config issue — should be GIVE_UP."""
        from src.consumers.deploy import _classify_deploy_failure

        mock_response = AsyncMock()
        mock_response.content = "GIVE_UP"

        with patch("src.consumers.deploy.ChatOpenAI") as mock_llm_cls:
            mock_llm = AsyncMock()
            mock_llm.ainvoke.return_value = mock_response
            mock_llm_cls.return_value = mock_llm

            result = await _classify_deploy_failure("port is already allocated on 0.0.0.0:8012")
            assert result == "GIVE_UP"

    @pytest.mark.asyncio
    async def test_import_error_classified_as_code_fix(self):
        """Import error is a code bug — should be CODE_FIX."""
        from src.consumers.deploy import _classify_deploy_failure

        mock_response = AsyncMock()
        mock_response.content = "CODE_FIX"

        with patch("src.consumers.deploy.ChatOpenAI") as mock_llm_cls:
            mock_llm = AsyncMock()
            mock_llm.ainvoke.return_value = mock_response
            mock_llm_cls.return_value = mock_llm

            result = await _classify_deploy_failure("ModuleNotFoundError: No module named 'foo'")
            assert result == "CODE_FIX"

    @pytest.mark.asyncio
    async def test_ssh_timeout_classified_as_retry(self):
        """SSH timeout is transient — should be RETRY."""
        from src.consumers.deploy import _classify_deploy_failure

        mock_response = AsyncMock()
        mock_response.content = "RETRY"

        with patch("src.consumers.deploy.ChatOpenAI") as mock_llm_cls:
            mock_llm = AsyncMock()
            mock_llm.ainvoke.return_value = mock_response
            mock_llm_cls.return_value = mock_llm

            result = await _classify_deploy_failure("SSH connection timed out after 30s")
            assert result == "RETRY"

    @pytest.mark.asyncio
    async def test_fallback_on_llm_failure_is_retry(self):
        """When LLM call fails, fallback should be RETRY (not CODE_FIX)."""
        from src.consumers.deploy import _classify_deploy_failure

        with patch("src.consumers.deploy.ChatOpenAI") as mock_llm_cls:
            mock_llm = AsyncMock()
            mock_llm.ainvoke.side_effect = RuntimeError("API error")
            mock_llm_cls.return_value = mock_llm

            result = await _classify_deploy_failure("some error")
            assert result == "RETRY"

    @pytest.mark.asyncio
    async def test_fallback_on_missing_api_key_is_retry(self, monkeypatch):
        """When OPEN_ROUTER_KEY is not set, fallback should be RETRY."""
        from src.consumers.deploy import _classify_deploy_failure

        monkeypatch.delenv("OPEN_ROUTER_KEY", raising=False)
        result = await _classify_deploy_failure("some error")
        assert result == "RETRY"

    @pytest.mark.asyncio
    async def test_unexpected_llm_response_is_retry(self):
        """When LLM returns unexpected value, fallback should be RETRY."""
        from src.consumers.deploy import _classify_deploy_failure

        mock_response = AsyncMock()
        mock_response.content = "BANANA"

        with patch("src.consumers.deploy.ChatOpenAI") as mock_llm_cls:
            mock_llm = AsyncMock()
            mock_llm.ainvoke.return_value = mock_response
            mock_llm_cls.return_value = mock_llm

            result = await _classify_deploy_failure("some error")
            assert result == "RETRY"

    @pytest.mark.asyncio
    async def test_prompt_contains_three_categories(self):
        """Verify the classify prompt mentions all three categories."""
        from src.consumers.deploy import CLASSIFY_PROMPT

        assert "RETRY" in CLASSIFY_PROMPT
        assert "CODE_FIX" in CLASSIFY_PROMPT
        assert "GIVE_UP" in CLASSIFY_PROMPT

    @pytest.mark.asyncio
    async def test_model_id_is_valid(self):
        """Model ID should be valid OpenRouter format (no date suffix)."""
        from src.consumers.deploy import _classify_deploy_failure

        with patch("src.consumers.deploy.ChatOpenAI") as mock_llm_cls:
            mock_response = AsyncMock()
            mock_response.content = "RETRY"
            mock_llm = AsyncMock()
            mock_llm.ainvoke.return_value = mock_response
            mock_llm_cls.return_value = mock_llm

            await _classify_deploy_failure("test")

            # Check the model kwarg passed to ChatOpenAI
            call_kwargs = mock_llm_cls.call_args[1]
            model = call_kwargs["model"]
            # Should NOT contain the invalid date-suffixed ID
            assert "20251001" not in model
            assert "claude-haiku" in model.lower() or "haiku" in model.lower()
