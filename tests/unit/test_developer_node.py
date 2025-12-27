"""Unit tests for DeveloperNode.

Tests dataclass attribute access for SpawnResult.
"""

from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage
import pytest

from services.langgraph.src.clients.worker_spawner import SpawnResult
from services.langgraph.src.nodes.developer import DeveloperNode


@pytest.mark.asyncio
async def test_spawn_worker_success():
    """Test spawn_worker with successful SpawnResult."""
    node = DeveloperNode()

    # Mock state
    state = {
        "repo_info": {"full_name": "test-org/test-repo", "name": "test-repo"},
        "project_spec": {"name": "Test Project", "description": "Test description"},
    }

    # Mock successful spawn result (dataclass, not dict)
    success_result = SpawnResult(
        request_id="test-123",
        success=True,
        exit_code=0,
        output="Worker completed successfully",
        commit_sha="abc123def456",
        branch="main",
        files_changed=["src/main.py", "README.md"],
        summary="Implemented feature",
    )

    with patch("services.langgraph.src.nodes.developer.GitHubAppClient") as mock_gh:
        mock_gh_instance = AsyncMock()
        mock_gh_instance.get_token.return_value = "ghs_test_token"
        mock_gh.return_value = mock_gh_instance

        with patch("services.langgraph.src.nodes.developer.request_spawn") as mock_spawn:
            mock_spawn.return_value = success_result

            result = await node.spawn_worker(state)

            # Verify result structure
            assert "messages" in result
            assert len(result["messages"]) == 1
            assert isinstance(result["messages"][0], AIMessage)

            # Verify message content includes commit info (dataclass attributes)
            message_content = result["messages"][0].content
            assert "✅" in message_content
            assert "abc123def456" in message_content  # commit_sha
            assert "main" in message_content  # branch

            # Verify worker_info contains dataclass
            assert result["worker_info"] == success_result


@pytest.mark.asyncio
async def test_spawn_worker_failure():
    """Test spawn_worker with failed SpawnResult."""
    node = DeveloperNode()

    state = {
        "repo_info": {"full_name": "test-org/test-repo", "name": "test-repo"},
        "project_spec": {"name": "Test Project", "description": "Test description"},
    }

    # Mock failed spawn result with error_message attribute
    failure_result = SpawnResult(
        request_id="test-456",
        success=False,
        exit_code=1,
        output="Worker failed",
        error_type="TimeoutError",
        error_message="Worker timed out after 600s",
    )

    with patch("services.langgraph.src.nodes.developer.GitHubAppClient") as mock_gh:
        mock_gh_instance = AsyncMock()
        mock_gh_instance.get_token.return_value = "ghs_test_token"
        mock_gh.return_value = mock_gh_instance

        with patch("services.langgraph.src.nodes.developer.request_spawn") as mock_spawn:
            mock_spawn.return_value = failure_result

            result = await node.spawn_worker(state)

            # Verify error message uses error_message attribute, not .get("error")
            assert "messages" in result
            message_content = result["messages"][0].content
            assert "❌" in message_content
            assert "Worker timed out after 600s" in message_content


@pytest.mark.asyncio
async def test_spawn_worker_no_repo_info():
    """Test spawn_worker handles missing repo_info."""
    node = DeveloperNode()

    state = {}

    result = await node.spawn_worker(state)

    assert "messages" in result
    message_content = result["messages"][0].content
    assert "❌" in message_content
    assert "No repository info found" in message_content


@pytest.mark.asyncio
async def test_spawn_worker_invalid_repo_name():
    """Test spawn_worker handles invalid repo full_name format."""
    node = DeveloperNode()

    state = {
        "repo_info": {
            "full_name": "invalid-repo-name",  # Missing org/repo format
            "name": "invalid-repo-name",
        }
    }

    with patch("services.langgraph.src.nodes.developer.GitHubAppClient"):
        result = await node.spawn_worker(state)

        assert "messages" in result
        message_content = result["messages"][0].content
        assert "❌" in message_content
        assert "Invalid repository full_name" in message_content
