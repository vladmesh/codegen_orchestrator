"""Integration tests for git SHA extraction with real git repos.

No mocks — exercises _get_git_head and _extract_git_commit_sha
against actual git repositories created in tmp directories.
"""

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

    # Create initial commit
    init_file = os.path.join(path, "README.md")
    with open(init_file, "w") as f:
        f.write("# Test\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial commit")
    return _git(path, "rev-parse", "HEAD")


class TestGetGitHeadReal:
    def test_returns_sha_in_real_repo(self, wrapper, tmp_path, monkeypatch):
        """_get_git_head returns real SHA from an actual git repo."""
        monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", str(tmp_path))
        initial_sha = _init_repo(str(tmp_path))

        result = wrapper._get_git_head()

        assert result == initial_sha
        assert len(result) == 40  # noqa: PLR2004
        assert all(c in "0123456789abcdef" for c in result)

    def test_returns_none_for_non_repo(self, wrapper, tmp_path, monkeypatch):
        """_get_git_head returns None for a directory that's not a git repo."""
        monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", str(tmp_path))

        result = wrapper._get_git_head()

        assert result is None

    def test_returns_none_for_nonexistent_dir(self, wrapper, monkeypatch):
        """_get_git_head returns None when WORKSPACE_DIR doesn't exist."""
        monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", "/nonexistent/path")

        result = wrapper._get_git_head()

        assert result is None


class TestExtractGitCommitShaReal:
    def test_detects_new_commit(self, wrapper, tmp_path, monkeypatch):
        """Detects that HEAD changed after a new commit."""
        monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", str(tmp_path))
        initial_sha = _init_repo(str(tmp_path))

        # Make a second commit (simulating what the agent would do)
        new_file = os.path.join(str(tmp_path), "new_code.py")
        with open(new_file, "w") as f:
            f.write("print('hello')\n")
        _git(str(tmp_path), "add", ".")
        _git(str(tmp_path), "commit", "-m", "agent work")

        result = wrapper._extract_git_commit_sha(initial_sha)

        assert result is not None
        assert result != initial_sha
        assert len(result) == 40  # noqa: PLR2004

    def test_returns_none_when_no_new_commit(self, wrapper, tmp_path, monkeypatch):
        """Returns None when HEAD is unchanged (agent made no commit)."""
        monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", str(tmp_path))
        initial_sha = _init_repo(str(tmp_path))

        result = wrapper._extract_git_commit_sha(initial_sha)

        assert result is None

    def test_detects_first_commit_from_empty_repo(self, wrapper, tmp_path, monkeypatch):
        """When initial_head was None (empty repo) and agent made first commit."""
        monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", str(tmp_path))

        _git(str(tmp_path), "init")
        _git(str(tmp_path), "config", "user.email", "test@test.com")
        _git(str(tmp_path), "config", "user.name", "Test")

        # initial_head would be None (empty repo has no HEAD)
        # Agent makes first commit
        init_file = os.path.join(str(tmp_path), "README.md")
        with open(init_file, "w") as f:
            f.write("# New project\n")
        _git(str(tmp_path), "add", ".")
        _git(str(tmp_path), "commit", "-m", "first commit by agent")

        result = wrapper._extract_git_commit_sha(initial_head=None)

        assert result is not None
        assert len(result) == 40  # noqa: PLR2004

    def test_multiple_commits_returns_latest(self, wrapper, tmp_path, monkeypatch):
        """When agent makes multiple commits, returns the latest HEAD."""
        monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", str(tmp_path))
        initial_sha = _init_repo(str(tmp_path))

        # Agent makes two commits
        for i in range(2):
            f_path = os.path.join(str(tmp_path), f"file_{i}.py")
            with open(f_path, "w") as f:
                f.write(f"# file {i}\n")
            _git(str(tmp_path), "add", ".")
            _git(str(tmp_path), "commit", "-m", f"commit {i}")

        expected_head = _git(str(tmp_path), "rev-parse", "HEAD")
        result = wrapper._extract_git_commit_sha(initial_sha)

        assert result == expected_head
        assert result != initial_sha
