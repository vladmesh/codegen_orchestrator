from unittest.mock import AsyncMock, MagicMock

import pytest
from workers_spawner.factories.agents.claude_code import ClaudeCodeAgent
from workers_spawner.factories.agents.factory_droid import FactoryDroidAgent


@pytest.mark.asyncio
async def test_claude_send_message_success():
    """Test ClaudeCodeAgent.send_message success path."""
    # Create mock container service
    mock_container_service = AsyncMock()
    mock_container_service.send_command.return_value = MagicMock(
        output='{"result": "Hi there", "session_id": "sid-123"}', exit_code=0, success=True
    )

    # Inject dependency
    agent = ClaudeCodeAgent(mock_container_service)
    agent_id = "test-agent"
    message = "Hello"

    response = await agent.send_message(agent_id, message)

    assert response["response"] == "Hi there"
    assert response["session_context"]["session_id"] == "sid-123"

    mock_container_service.send_command.assert_called_once()
    args = mock_container_service.send_command.call_args
    cmd = args[0][1]
    assert "claude" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "-p 'Hello'" in cmd
    assert "--output-format json" in cmd


@pytest.mark.asyncio
async def test_claude_send_message_with_session():
    """Test ClaudeCodeAgent.send_message with existing session."""
    mock_container_service = AsyncMock()
    mock_container_service.send_command.return_value = MagicMock(
        output='{"result": "Continued", "session_id": "sid-old"}', exit_code=0, success=True
    )

    agent = ClaudeCodeAgent(mock_container_service)
    session_context = {"session_id": "sid-old"}

    await agent.send_message("id", "msg", session_context)

    args = mock_container_service.send_command.call_args
    cmd = args[0][1]
    assert "--resume sid-old" in cmd


@pytest.mark.asyncio
async def test_claude_send_message_json_error():
    """Test ClaudeCodeAgent.send_message with non-JSON output."""
    mock_container_service = AsyncMock()
    mock_container_service.send_command.return_value = MagicMock(
        output="Fatal error: something went wrong", exit_code=1, success=False
    )

    agent = ClaudeCodeAgent(mock_container_service)

    response = await agent.send_message("id", "msg")

    assert response["response"] == "Fatal error: something went wrong"
    assert response["metadata"]["parse_error"] is True


@pytest.mark.asyncio
async def test_factory_droid_not_implemented():
    """Test FactoryDroidAgent raises NotImplementedError."""
    mock_container_service = AsyncMock()
    agent = FactoryDroidAgent(mock_container_service)
    with pytest.raises(NotImplementedError):
        await agent.send_message("id", "msg")
