"""Integration test: deploy failure → classify → route to correct handler.

Tests the full flow from classification through routing, verifying that:
- port conflict → GIVE_UP → no engineering dispatch
- import error → CODE_FIX → engineering dispatch
- SSH timeout → RETRY → retry counter (no engineering dispatch)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.queues.deploy import DeployMessage, DeployTrigger

_PATCH = "src.consumers.deploy_failure_handler"


def _make_deploy_msg(**overrides) -> dict:
    defaults = {
        "task_id": "deploy-flow-1",
        "project_id": "proj-1",
        "user_id": "123",
        "callback_stream": "cb:123",
        "triggered_by": DeployTrigger.ENGINEERING.value,
        "action": "create",
        "story_id": "story-1",
        "deploy_fix_attempt": 0,
    }
    defaults.update(overrides)
    return defaults


def _mock_llm_response(content: str):
    """Create a mock LLM that returns the given classification."""
    mock_response = AsyncMock()
    mock_response.content = content
    mock_llm = AsyncMock()
    mock_llm.ainvoke.return_value = mock_response
    return mock_llm


class TestDeployFailureFlow:
    """End-to-end flow: classify + route for each failure type."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("OPEN_ROUTER_KEY", "test-key")

    @pytest.mark.asyncio
    async def test_port_conflict_gives_up_no_engineering(self):
        """Port conflict → GIVE_UP → _handle_give_up, NO _redispatch."""
        from src.consumers.deploy_failure_handler import (
            _classify_deploy_failure,
            _route_deploy_failure,
        )

        msg = DeployMessage.model_validate(_make_deploy_msg())
        redis = MagicMock()
        redis.redis = AsyncMock()

        # Classify
        with patch(f"{_PATCH}.ChatOpenAI") as mock_cls:
            mock_cls.return_value = _mock_llm_response("GIVE_UP")
            classification = await _classify_deploy_failure(
                "port is already allocated on 0.0.0.0:8012"
            )

        assert classification == "GIVE_UP"

        # Route
        with (
            patch(f"{_PATCH}._handle_give_up", new_callable=AsyncMock) as mock_gu,
            patch(
                f"{_PATCH}._redispatch_to_engineering",
                new_callable=AsyncMock,
            ) as mock_rd,
        ):
            await _route_deploy_failure(
                classification=classification,
                redis=redis,
                msg=msg,
                error_details="port is already allocated",
                story_id="story-1",
            )
            mock_gu.assert_called_once()
            mock_rd.assert_not_called()

    @pytest.mark.asyncio
    async def test_import_error_dispatches_to_engineering(self):
        """Import error → CODE_FIX → _redispatch_to_engineering."""
        from src.consumers.deploy_failure_handler import (
            _classify_deploy_failure,
            _route_deploy_failure,
        )

        msg = DeployMessage.model_validate(_make_deploy_msg())
        redis = MagicMock()
        redis.redis = AsyncMock()

        with patch(f"{_PATCH}.ChatOpenAI") as mock_cls:
            mock_cls.return_value = _mock_llm_response("CODE_FIX")
            classification = await _classify_deploy_failure(
                "ModuleNotFoundError: No module named 'requests'"
            )

        assert classification == "CODE_FIX"

        with (
            patch(f"{_PATCH}._handle_give_up", new_callable=AsyncMock) as mock_gu,
            patch(
                f"{_PATCH}._redispatch_to_engineering",
                new_callable=AsyncMock,
            ) as mock_rd,
            patch(
                f"{_PATCH}._transition_story_safe",
                new_callable=AsyncMock,
            ),
        ):
            mock_rd.return_value = True
            await _route_deploy_failure(
                classification=classification,
                redis=redis,
                msg=msg,
                error_details="ModuleNotFoundError",
                story_id="story-1",
            )
            mock_rd.assert_called_once()
            mock_gu.assert_not_called()

    @pytest.mark.asyncio
    async def test_ssh_timeout_retries_no_engineering(self):
        """SSH timeout → RETRY → no engineering dispatch, no give_up."""
        from src.consumers.deploy_failure_handler import (
            _classify_deploy_failure,
            _route_deploy_failure,
        )

        msg = DeployMessage.model_validate(_make_deploy_msg())
        redis = MagicMock()
        redis.redis = AsyncMock()

        with patch(f"{_PATCH}.ChatOpenAI") as mock_cls:
            mock_cls.return_value = _mock_llm_response("RETRY")
            classification = await _classify_deploy_failure("SSH connection timed out after 30s")

        assert classification == "RETRY"

        with (
            patch(f"{_PATCH}._handle_give_up", new_callable=AsyncMock) as mock_gu,
            patch(
                f"{_PATCH}._redispatch_to_engineering",
                new_callable=AsyncMock,
            ) as mock_rd,
        ):
            await _route_deploy_failure(
                classification=classification,
                redis=redis,
                msg=msg,
                error_details="SSH timeout",
                story_id="story-1",
            )
            mock_rd.assert_not_called()
            mock_gu.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_failure_defaults_to_retry(self):
        """LLM crash → fallback RETRY → no engineering dispatch."""
        from src.consumers.deploy_failure_handler import _classify_deploy_failure

        with patch(f"{_PATCH}.ChatOpenAI") as mock_cls:
            mock_llm = AsyncMock()
            mock_llm.ainvoke.side_effect = RuntimeError("API 500")
            mock_cls.return_value = mock_llm
            classification = await _classify_deploy_failure("any error")

        assert classification == "RETRY"
