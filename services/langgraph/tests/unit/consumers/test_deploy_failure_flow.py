"""Unit test: deploy failure classification → correct deploy_outcome stored.

Tests that the classify → outcome mapping works correctly, and that
_handle_deploy_failure stores the right deploy_outcome in run.result.
"""

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.queues.deploy import DeployOutcome

_PATCH = "src.consumers.deploy_failure_handler"


def _mock_llm_response(content: str):
    """Create a mock LLM that returns the given classification."""
    mock_response = AsyncMock()
    mock_response.content = content
    mock_llm = AsyncMock()
    mock_llm.ainvoke.return_value = mock_response
    return mock_llm


class TestDeployFailureClassification:
    """Classification + outcome mapping for each failure type."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("OPEN_ROUTER_KEY", "test-key")

    @pytest.mark.asyncio
    async def test_port_conflict_classifies_as_give_up(self):
        """Port conflict → GIVE_UP classification."""
        from src.consumers.deploy_failure_handler import _classify_deploy_failure

        with patch(f"{_PATCH}.ChatOpenAI") as mock_cls:
            mock_cls.return_value = _mock_llm_response("GIVE_UP")
            classification = await _classify_deploy_failure(
                "port is already allocated on 0.0.0.0:8012"
            )

        assert classification == "GIVE_UP"

    @pytest.mark.asyncio
    async def test_import_error_classifies_as_code_fix(self):
        """Import error → CODE_FIX classification."""
        from src.consumers.deploy_failure_handler import _classify_deploy_failure

        with patch(f"{_PATCH}.ChatOpenAI") as mock_cls:
            mock_cls.return_value = _mock_llm_response("CODE_FIX")
            classification = await _classify_deploy_failure(
                "ModuleNotFoundError: No module named 'requests'"
            )

        assert classification == "CODE_FIX"

    @pytest.mark.asyncio
    async def test_ssh_timeout_classifies_as_retry(self):
        """SSH timeout → RETRY classification."""
        from src.consumers.deploy_failure_handler import _classify_deploy_failure

        with patch(f"{_PATCH}.ChatOpenAI") as mock_cls:
            mock_cls.return_value = _mock_llm_response("RETRY")
            classification = await _classify_deploy_failure("SSH connection timed out after 30s")

        assert classification == "RETRY"

    @pytest.mark.asyncio
    async def test_llm_failure_defaults_to_retry(self):
        """LLM crash → fallback RETRY."""
        from src.consumers.deploy_failure_handler import _classify_deploy_failure

        with patch(f"{_PATCH}.ChatOpenAI") as mock_cls:
            mock_llm = AsyncMock()
            mock_llm.ainvoke.side_effect = RuntimeError("API 500")
            mock_cls.return_value = mock_llm
            classification = await _classify_deploy_failure("any error")

        assert classification == "RETRY"


class TestClassificationToOutcome:
    """_classification_to_outcome maps strings to DeployOutcome enum."""

    def test_code_fix(self):
        from src.consumers.deploy_failure_handler import _classification_to_outcome

        assert _classification_to_outcome("CODE_FIX") == DeployOutcome.CODE_FIX

    def test_retry(self):
        from src.consumers.deploy_failure_handler import _classification_to_outcome

        assert _classification_to_outcome("RETRY") == DeployOutcome.RETRY

    def test_give_up(self):
        from src.consumers.deploy_failure_handler import _classification_to_outcome

        assert _classification_to_outcome("GIVE_UP") == DeployOutcome.GIVE_UP

    def test_unknown_defaults_to_retry(self):
        from src.consumers.deploy_failure_handler import _classification_to_outcome

        assert _classification_to_outcome("UNKNOWN") == DeployOutcome.RETRY


class TestHandleDeployFailure:
    """_handle_deploy_failure stores deploy_outcome in run.result."""

    @pytest.mark.asyncio
    async def test_stores_outcome_in_run_result(self):
        from src.consumers.deploy_failure_handler import _handle_deploy_failure

        mock_redis = AsyncMock()

        with patch(f"{_PATCH}.api_client") as mock_api:
            mock_api.patch = AsyncMock()
            result = await _handle_deploy_failure(
                task_id="deploy-1",
                project_id="proj-1",
                error_msg="SSH timeout",
                story_id="story-1",
                callback_stream="cb:1",
                user_id="123",
                redis=mock_redis,
                deploy_outcome=DeployOutcome.RETRY,
                deploy_fix_attempt=1,
            )

            # Verify run was patched with deploy_outcome
            patch_call = mock_api.patch.call_args_list[0]
            assert patch_call[0][0] == "runs/deploy-1"
            run_result = patch_call[1]["json"]["result"]
            assert run_result["deploy_outcome"] == DeployOutcome.RETRY.value
            assert run_result["error_details"] == "SSH timeout"
            assert run_result["deploy_fix_attempt"] == 1

        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_does_not_call_transition_story(self):
        """Deploy worker must NOT transition stories — dispatcher does that."""
        from src.consumers.deploy_failure_handler import _handle_deploy_failure

        mock_redis = AsyncMock()

        with patch(f"{_PATCH}.api_client") as mock_api:
            mock_api.patch = AsyncMock()
            await _handle_deploy_failure(
                task_id="deploy-1",
                project_id="proj-1",
                error_msg="error",
                story_id="story-1",
                callback_stream="cb:1",
                user_id="123",
                redis=mock_redis,
            )

            # Verify no transition_story calls
            for call in mock_api.method_calls:
                assert "transition_story" not in str(call)
