"""Unit tests for git branch detection in WorkerWrapper."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from worker_wrapper.wrapper import WorkerWrapper, WorkerWrapperConfig


@pytest.fixture
def wrapper_config():
    return WorkerWrapperConfig(
        redis_url="redis://localhost",
        input_stream="in",
        output_stream="out",
        consumer_group="grp",
        consumer_name="worker-1",
        agent_type="claude",
    )


@pytest.fixture
def wrapper(wrapper_config):
    mock_redis = MagicMock()
    mock_redis.redis = AsyncMock()
    return WorkerWrapper(config=wrapper_config, redis_client=mock_redis)


class TestGetGitBranch:
    def test_returns_branch_name(self, wrapper):
        """_get_git_branch returns current branch name."""
        mock_result = MagicMock()
        mock_result.stdout = "story/story-123\n"
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result):
            result = wrapper._get_git_branch()

        assert result == "story/story-123"

    def test_returns_none_for_detached_head(self, wrapper):
        """_get_git_branch returns None when HEAD is detached."""
        mock_result = MagicMock()
        mock_result.stdout = "HEAD\n"
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result):
            result = wrapper._get_git_branch()

        assert result is None

    def test_returns_none_on_failure(self, wrapper):
        """_get_git_branch returns None when git command fails."""
        mock_result = MagicMock()
        mock_result.returncode = 128
        mock_result.stderr = "fatal: not a git repository"

        with patch("subprocess.run", return_value=mock_result):
            result = wrapper._get_git_branch()

        assert result is None

    def test_returns_none_on_exception(self, wrapper):
        """_get_git_branch returns None on any exception."""
        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            result = wrapper._get_git_branch()

        assert result is None
