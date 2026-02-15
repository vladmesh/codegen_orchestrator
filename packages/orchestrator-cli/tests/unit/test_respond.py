"""Unit tests for orchestrator respond command."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_redis_module():
    """Ensure redis.asyncio is importable in test environment."""
    if "redis.asyncio" not in sys.modules:
        mock_redis = MagicMock()
        sys.modules.setdefault("redis", mock_redis)
        sys.modules.setdefault("redis.asyncio", mock_redis)


@pytest.fixture
def mock_redis():
    return AsyncMock()


class TestSendResponseAsync:
    @pytest.mark.asyncio
    async def test_writes_to_po_input(self, mock_redis):
        from orchestrator_cli.commands.respond import send_response_async

        with (
            patch.dict(
                os.environ,
                {"ORCHESTRATOR_AGENT_ID": "agent-1", "ORCHESTRATOR_USER_ID": "user-42"},
            ),
            patch(
                "orchestrator_cli.commands.respond.get_redis_client",
                return_value=mock_redis,
            ),
        ):
            await send_response_async("Task completed")

        mock_redis.xadd.assert_called_once()
        stream, data = mock_redis.xadd.call_args[0]
        assert stream == "po:input"
        assert data["type"] == "system_event"
        assert data["event"] == "agent_message"
        assert data["user_id"] == "user-42"
        assert "[agent:agent-1]" in data["text"]
        assert "Task completed" in data["text"]
        assert "timestamp" in data

    @pytest.mark.asyncio
    async def test_closes_redis_on_success(self, mock_redis):
        from orchestrator_cli.commands.respond import send_response_async

        with (
            patch.dict(os.environ, {"ORCHESTRATOR_AGENT_ID": "agent-1"}),
            patch(
                "orchestrator_cli.commands.respond.get_redis_client",
                return_value=mock_redis,
            ),
        ):
            await send_response_async("done")

        mock_redis.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_closes_redis_on_error(self, mock_redis):
        from orchestrator_cli.commands.respond import send_response_async

        mock_redis.xadd.side_effect = ConnectionError("redis down")
        with (
            patch.dict(os.environ, {"ORCHESTRATOR_AGENT_ID": "agent-1"}),
            patch(
                "orchestrator_cli.commands.respond.get_redis_client",
                return_value=mock_redis,
            ),
        ):
            with pytest.raises(ConnectionError):
                await send_response_async("fail")

        mock_redis.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_defaults_user_id_to_unknown(self, mock_redis):
        from orchestrator_cli.commands.respond import send_response_async

        env = {"ORCHESTRATOR_AGENT_ID": "agent-1"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch(
                "orchestrator_cli.commands.respond.get_redis_client",
                return_value=mock_redis,
            ),
        ):
            os.environ.pop("ORCHESTRATOR_USER_ID", None)
            await send_response_async("hello")

        data = mock_redis.xadd.call_args[0][1]
        assert data["user_id"] == "unknown"
