"""Integration tests for git branch detection with real git repos."""

import os
import subprocess
from unittest.mock import MagicMock

import pytest
from worker_wrapper.workspace_overlay import WorkspaceOverlay
from worker_wrapper.wrapper import WorkerWrapper, WorkerWrapperConfig

from shared.contracts.queues.worker_result import WorkerCompletedResult


@pytest.fixture
def wrapper_config():
    return WorkerWrapperConfig(
        broker_url="http://worker-broker:8001",
        broker_token="x" * 43,
        worker_id="test-worker",
        agent_type="claude",
    )


@pytest.fixture
def wrapper(wrapper_config):
    mock_redis = MagicMock()
    mock_redis.redis = MagicMock()
    return WorkerWrapper(config=wrapper_config, broker_client=mock_redis)


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


@pytest.mark.parametrize("instruction_name", ["AGENTS.md", "CLAUDE.md"])
def test_completed_result_pushes_only_the_sanitized_product_tree(
    wrapper, tmp_path, monkeypatch, instruction_name
):
    """Reproduce an agent `git add -A` commit and inspect the exact bare-remote tree."""
    remote = tmp_path / "remote.git"
    workspace = tmp_path / "workspace"
    remote.mkdir()
    workspace.mkdir()
    _git(str(remote), "init", "--bare")
    _git(str(workspace), "init", "-b", "story/test")
    _git(str(workspace), "config", "user.email", "test@test.com")
    _git(str(workspace), "config", "user.name", "Test")
    (workspace / "AGENTS.md").write_text("product agents\n")
    (workspace / "CLAUDE.md").write_text("product claude\n")
    (workspace / "Makefile").write_text("worker-start:\n\t@docker compose up\n")
    (workspace / "product.py").write_text("VALUE = 1\n")
    _git(str(workspace), "add", "-A")
    _git(str(workspace), "commit", "-m", "initial")
    _git(str(workspace), "remote", "add", "origin", str(remote))
    _git(str(workspace), "push", "-u", "origin", "story/test")

    overlay = WorkspaceOverlay(workspace)
    overlay.configure_instruction(instruction_name, "dynamic instructions\n")
    overlay.activate(task="task\n", story="story\n")
    (workspace / "PROGRESS.md").write_text("progress\n")
    (workspace / ".venv_paths_fixed").touch()
    (workspace / "product.py").write_text("VALUE = 2\n")
    _git(str(workspace), "add", "-A")
    _git(str(workspace), "commit", "-m", "agent product edit")
    reported = _git(str(workspace), "rev-parse", "HEAD")
    monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", str(workspace))

    result, error = wrapper._pushed_completed_result(
        WorkerCompletedResult(commit_sha=reported, content="done"), "story/test"
    )

    assert error is None
    assert result is not None
    assert result.commit_sha != reported
    assert _git(str(remote), "rev-parse", "refs/heads/story/test") == result.commit_sha
    tree = set(_git(str(remote), "ls-tree", "-r", "--name-only", result.commit_sha).splitlines())
    assert "product.py" in tree
    assert not {"TASK.md", ".story/STORY.md", "PROGRESS.md", ".venv_paths_fixed"} & tree
    assert "dynamic instructions" not in _git(
        str(remote), "show", f"{result.commit_sha}:{instruction_name}"
    )
    assert "orchestrator overrides" not in _git(
        str(remote), "show", f"{result.commit_sha}:Makefile"
    )
