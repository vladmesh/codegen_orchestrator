"""Integration tests for git branch detection with real git repos."""

import os
import subprocess
from unittest.mock import MagicMock

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
    mock_redis.redis = MagicMock()
    return WorkerWrapper(config=wrapper_config, redis_client=mock_redis)


def _git(repo_path: str, *args: str) -> str:
    """Run git command in repo_path, return stdout."""
    result = subprocess.run(
        ["/usr/bin/git", *args],  # noqa: S603
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _init_repo(path: str) -> str:
    """Initialize a git repo with one commit. Returns initial HEAD SHA."""
    _git(path, "init")
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")

    init_file = os.path.join(path, "README.md")
    with open(init_file, "w") as f:
        f.write("# Test\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial commit")
    return _git(path, "rev-parse", "HEAD")


class TestGetGitBranchReal:
    def test_returns_default_branch(self, wrapper, tmp_path, monkeypatch):
        """_get_git_branch returns the default branch name in a real repo."""
        monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", str(tmp_path))
        _init_repo(str(tmp_path))

        result = wrapper._get_git_branch()

        assert result is not None
        assert result in ("main", "master")

    def test_returns_feature_branch(self, wrapper, tmp_path, monkeypatch):
        """_get_git_branch returns the feature branch after checkout."""
        monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", str(tmp_path))
        _init_repo(str(tmp_path))
        _git(str(tmp_path), "checkout", "-b", "story/story-123")

        result = wrapper._get_git_branch()

        assert result == "story/story-123"

    def test_returns_none_for_detached_head(self, wrapper, tmp_path, monkeypatch):
        """_get_git_branch returns None when HEAD is detached."""
        monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", str(tmp_path))
        sha = _init_repo(str(tmp_path))
        _git(str(tmp_path), "checkout", sha)

        result = wrapper._get_git_branch()

        assert result is None

    def test_returns_none_for_non_repo(self, wrapper, tmp_path, monkeypatch):
        """_get_git_branch returns None for a non-git directory."""
        monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", str(tmp_path))

        result = wrapper._get_git_branch()

        assert result is None
