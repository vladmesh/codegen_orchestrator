from unittest.mock import AsyncMock

import pytest
from workers_spawner.session_manager import AgentSessionManager


@pytest.mark.asyncio
async def test_session_manager_save_get():
    """Test saving and getting session context."""
    mock_redis = AsyncMock()
    mock_redis.get.return_value = '{"session_id": "sid-123"}'

    manager = AgentSessionManager(mock_redis)
    result = await manager.get_session_context("agent-1")

    assert result == {"session_id": "sid-123"}
    mock_redis.get.assert_called_with("agent_session:agent-1")


@pytest.mark.asyncio
async def test_session_manager_save():
    """Test saving session context."""
    mock_redis = AsyncMock()

    manager = AgentSessionManager(mock_redis)
    await manager.save_session_context("agent-1", {"key": "value"})

    mock_redis.set.assert_called_with("agent_session:agent-1", '{"key": "value"}', ex=7200)


@pytest.mark.asyncio
async def test_session_manager_get_missing():
    """Test getting missing session."""
    mock_redis = AsyncMock()
    mock_redis.get.return_value = None

    manager = AgentSessionManager(mock_redis)
    result = await manager.get_session_context("agent-missing")

    assert result is None


@pytest.mark.asyncio
async def test_session_manager_delete():
    """Test deleting session."""
    mock_redis = AsyncMock()

    manager = AgentSessionManager(mock_redis)
    await manager.delete_session_context("agent-1")

    mock_redis.delete.assert_called_with("agent_session:agent-1")
